"""Single-host durable DAG dispatch. Gateway owns acceptance; no model can close cards."""
import copy
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from .gateway import canonical, digest, fields, identifier, require, Rejected


HOSTS = ('dsh', 'claude', 'codex', 'hermes', 'openclaw', 'qclaw', 'workbuddy', 'trae')
ACTIVE = ('launching', 'running', 'unknown', 'cancel_requested')
TERMINAL_EXECUTION = ('awaiting_validation', 'accepted_pending_sync', 'done', 'failed')


def validate_plan(plan, read_task):
    fields(plan, 'planId revision title maxConcurrency members nodes')
    require(identifier(plan['planId']) and type(plan['revision']) is int and plan['revision'] > 0, 'INVALID_PLAN', 400)
    require(isinstance(plan['title'], str) and 0 < len(plan['title']) <= 200, 'INVALID_TITLE', 400)
    require(type(plan['maxConcurrency']) is int and 1 <= plan['maxConcurrency'] <= 16, 'INVALID_CONCURRENCY', 400)
    require(isinstance(plan['members'], list) and 0 < len(plan['members']) <= 64, 'INVALID_MEMBERS', 400)
    members = {}
    for member in plan['members']:
        fields(member, 'memberId host roles maxConcurrency')
        require(identifier(member['memberId']) and member['memberId'] not in members and member['host'] in HOSTS, 'INVALID_MEMBER', 400)
        require(isinstance(member['roles'], list) and member['roles'] and all(identifier(r) for r in member['roles']), 'INVALID_ROLES', 400)
        require(type(member['maxConcurrency']) is int and 1 <= member['maxConcurrency'] <= 16, 'INVALID_MEMBER_CAPACITY', 400)
        members[member['memberId']] = member
    require(isinstance(plan['nodes'], list) and 0 < len(plan['nodes']) <= 200, 'INVALID_NODES', 400)
    nodes, workspaces, contracts = {}, [], {}
    for node in plan['nodes']:
        fields(node, 'taskId title memberId role dependsOn')
        require(identifier(node['taskId']) and node['taskId'] not in nodes, 'DUPLICATE_OR_INVALID_NODE', 400)
        require(isinstance(node['title'], str) and 0 < len(node['title']) <= 200, 'INVALID_TITLE', 400)
        require(node['memberId'] in members and node['role'] in members[node['memberId']]['roles'], 'MEMBER_CAPABILITY_MISMATCH', 400)
        require(isinstance(node['dependsOn'], list) and all(identifier(n) for n in node['dependsOn']) and len(set(node['dependsOn'])) == len(node['dependsOn']), 'INVALID_DEPENDENCIES', 400)
        current = read_task(node['taskId'])
        spec = current.get('contract')
        require(spec is not None and spec['taskId'] == node['taskId'] and spec['workerId'] == node['memberId'], 'TASK_BINDING_MISMATCH')
        require(current['state'] == 'todo' and current.get('attemptId') is None, 'TASK_ALREADY_STARTED')
        workspace = Path(spec['workspace'])
        require(workspace.is_absolute() and workspace.is_dir() and str(workspace.resolve()) == str(workspace), 'INVALID_WORKSPACE')
        require(all(workspace != old and workspace not in old.parents and old not in workspace.parents for old in workspaces), 'WORKSPACE_NOT_INDEPENDENT')
        workspaces.append(workspace)
        nodes[node['taskId']] = node
        contracts[node['taskId']] = spec
    visited, active = set(), set()
    def visit(task):
        require(task in nodes, 'MISSING_DEPENDENCY', 400)
        require(task not in active, 'DEPENDENCY_CYCLE', 400)
        if task in visited: return
        active.add(task)
        for dep in nodes[task]['dependsOn']: visit(dep)
        active.remove(task); visited.add(task)
    for task in nodes: visit(task)
    return contracts


class Scheduler:
    """Trusted callbacks: read_task(id), launch(node, member, attempt_id), optional stop(id, attempt).

    launch invokes runner.run_job, which alone consumes Gateway's atomic claim.
    Uncertain launch intents permanently reserve capacity until authoritative terminal receipt.
    """
    def __init__(self, state_dir, read_task, launch, max_concurrency=2, stop=None):
        self.root = Path(state_dir)
        require(self.root.is_absolute() and str(self.root.resolve()) == str(self.root), 'INVALID_SCHEDULER_STATE')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(self.root.stat().st_uid == os.getuid() and self.root.stat().st_mode & 0o077 == 0, 'INSECURE_SCHEDULER_STATE')
        require(type(max_concurrency) is int and 1 <= max_concurrency <= 16, 'INVALID_CONCURRENCY')
        self.read_task, self.launch, self.stop = read_task, launch, stop
        self.limit = max_concurrency
        self.db = self.root / 'scheduler.sqlite'
        require(not self.db.is_symlink() and (not self.db.exists() or self.db.stat().st_nlink == 1), 'UNSAFE_SCHEDULER_DATABASE')
        self.pool = ThreadPoolExecutor(max_workers=max_concurrency)
        self.futures, self.mutex = {}, threading.RLock()
        with self.transaction() as db:
            db.executescript('CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, body TEXT NOT NULL);'
                             'CREATE TABLE IF NOT EXISTS nodes (task TEXT PRIMARY KEY, plan TEXT NOT NULL, body TEXT NOT NULL);')
        os.chmod(self.db, 0o600)

    @contextmanager
    def transaction(self):
        from .recovery import state_lease
        with state_lease(self.root):
            for name in (str(self.db), str(self.db)+'-wal', str(self.db)+'-shm', str(self.db)+'-journal'):
                p = Path(name)
                require(not p.is_symlink() and (not p.exists() or p.stat().st_nlink == 1), 'UNSAFE_SCHEDULER_DATABASE')
            db = sqlite3.connect(str(self.db), timeout=10, isolation_level=None)
            try:
                db.execute('PRAGMA synchronous=FULL'); db.execute('BEGIN IMMEDIATE')
                yield db
                db.commit()
            except BaseException:
                db.rollback(); raise
            finally: db.close()

    @contextmanager
    def exclusive(self):
        with self.mutex:
            fd = os.open(str(self.root / 'scheduler.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                info = os.fstat(fd)
                require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, 'UNSAFE_SCHEDULER_LOCK')
                fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally: os.close(fd)

    def submit_plan(self, plan):
        # Exact redelivery does not require tasks to still be todo.
        with self.exclusive():
            with self.transaction() as db:
                old = db.execute('SELECT body FROM plans WHERE id=?', (plan.get('planId'),)).fetchone()
                if old:
                    require(json.loads(old[0])['contract'] == plan, 'IMMUTABLE_PLAN')
                    return plan['planId']
            contracts = validate_plan(plan, self.read_task)
            with self.transaction() as db:
                require(all(db.execute('SELECT 1 FROM nodes WHERE task=?', (n['taskId'],)).fetchone() is None for n in plan['nodes']), 'TASK_ALREADY_SCHEDULED')
                db.execute('INSERT INTO plans VALUES (?,?)', (plan['planId'], canonical({'contract': plan, 'status': 'active'})))
                for node in plan['nodes']:
                    record = {'node': node, 'contract': contracts[node['taskId']], 'state': 'queued', 'attemptId': None,
                              'cancelRequested': False, 'stopConfirmed': False, 'observation': None, 'error': None}
                    db.execute('INSERT INTO nodes VALUES (?,?,?)', (node['taskId'], plan['planId'], canonical(record)))
        return plan['planId']

    def _rows(self):
        with self.transaction() as db:
            plans = {r[0]: json.loads(r[1]) for r in db.execute('SELECT id,body FROM plans ORDER BY id')}
            rows = {r[0]: dict(json.loads(r[2]), planId=r[1]) for r in db.execute('SELECT task,plan,body FROM nodes ORDER BY task')}
        return plans, rows

    def _save(self, task, record):
        record = {k:v for k,v in record.items() if k != 'planId'}
        with self.transaction() as db:
            db.execute('UPDATE nodes SET body=? WHERE task=?', (canonical(record), task))

    def tick(self, dispatch=True):
        with self.exclusive():
            plans, rows = self._rows()
            for task, record in rows.items():
                try:
                    observed = self.read_task(task)
                    require(observed.get('contract') == record['contract'], 'SCHEDULER_CONTRACT_DRIFT')
                    if record['attemptId'] is not None:
                        require(observed.get('attemptId') in (None, record['attemptId']), 'SCHEDULER_ATTEMPT_DRIFT')
                    record['observation'] = observed
                    record['error'] = None
                    future = self.futures.get(task)
                    live = future is not None and not future.done()
                    if record['cancelRequested']:
                        if record['attemptId'] is None or (not live and observed['state'] in TERMINAL_EXECUTION):
                            record['state'] = 'cancelled'
                        else: record['state'] = 'cancel_requested'
                    elif observed['state'] in TERMINAL_EXECUTION:
                        record['state'] = observed['state']
                    elif record['attemptId']:
                        record['state'] = 'running' if live else 'unknown'
                        if not live: record['error'] = observed.get('dispatchError') or 'EXECUTION_UNCONFIRMED_NO_REPLAY'
                    elif observed['state'] != 'todo' or observed.get('attemptId'):
                        record['state'] = 'unknown'; record['error'] = 'EXTERNAL_DISPATCH_DETECTED'
                except Exception as exc:
                    record['error'] = exc.code if isinstance(exc, Rejected) else 'GATEWAY_UNREACHABLE'
                    # Keep old execution occupancy. A failed read never releases a running slot.
                self._save(task, record)
            if not dispatch:
                return self.snapshot()
            for task, record in rows.items():
                plan = plans[record['planId']]
                if plan['status'] != 'active' or record['state'] != 'queued' or record['error']: continue
                deps = [rows[d] for d in record['node']['dependsOn']]
                if any(d['state'] != 'done' or d['error'] or d['cancelRequested'] or (d['observation'] or {}).get('syncState') != 'synced' for d in deps): continue
                active = [r for r in rows.values() if r['state'] in ACTIVE]
                member_id = record['node']['memberId']
                member = next(m for m in plan['contract']['members'] if m['memberId'] == member_id)
                if len(active) >= self.limit: break
                if sum(r['planId'] == record['planId'] for r in active) >= plan['contract']['maxConcurrency']: continue
                if sum(r['node']['memberId'] == member_id for r in active) >= member['maxConcurrency']: continue
                # Freeze intent before launch. A crash here is unknown, never a replay.
                record['attemptId'] = 'dag-' + digest({'plan': plan['contract'], 'task': task})[:48]
                record['state'] = 'launching'
                self._save(task, record)
                try:
                    self.futures[task] = self.pool.submit(self.launch, copy.deepcopy(record['node']), copy.deepcopy(member), record['attemptId'])
                except Exception:
                    record['state'], record['error'] = 'unknown', 'LAUNCH_UNCONFIRMED_NO_REPLAY'
                    self._save(task, record)
            return self.snapshot()

    def cancel(self, task_id):
        with self.exclusive():
            plans, rows = self._rows()
            require(task_id in rows, 'TASK_NOT_FOUND', 404)
            targets = {task_id}
            while True:
                more = {t for t,r in rows.items() if any(d in targets for d in r['node']['dependsOn'])}
                if more <= targets: break
                targets |= more
            for task in sorted(targets):
                record = rows[task]
                record['cancelRequested'] = True
                record['state'] = 'cancelled' if record['attemptId'] is None else 'cancel_requested'
                self._save(task, record)
                if record['attemptId'] and self.stop:
                    # Request transport may fail; never infer OS process stopped from an ack.
                    try: self.stop(task, record['attemptId'])
                    except Exception:
                        record['error'] = 'CANCEL_DELIVERY_UNKNOWN'; self._save(task, record)
        return sorted(targets)

    def supersede(self, old_plan_id, new_plan):
        # New task IDs, upstream issues and workspaces are required; old contract is never edited.
        with self.exclusive():
            plans, rows = self._rows()
            require(old_plan_id in plans, 'PLAN_NOT_FOUND', 404)
            old = plans[old_plan_id]
            require(new_plan['planId'] != old_plan_id and type(new_plan['revision']) is int and new_plan['revision'] > old['contract']['revision'], 'NEW_REVISION_REQUIRED')
            require(not set(n['taskId'] for n in new_plan['nodes']) & set(rows), 'NEW_TASK_IDS_REQUIRED')
            contracts = validate_plan(new_plan, self.read_task)
            old_work = [Path(r['contract']['workspace']) for r in rows.values()]
            for spec in contracts.values():
                p = Path(spec['workspace'])
                require(all(p != w and p not in w.parents and w not in p.parents for w in old_work), 'REVISION_WORKSPACE_OVERLAP')
            # Pause the old plan first; a crash cannot start both plans automatically.
            old['status'] = 'superseded'
            with self.transaction() as db:
                db.execute('UPDATE plans SET body=? WHERE id=?', (canonical(old), old_plan_id))
        for task, row in rows.items():
            if row['planId'] == old_plan_id: self.cancel(task)
        return self.submit_plan(new_plan)

    def snapshot(self):
        plans, rows = self._rows()
        cards = []
        for task, row in rows.items():
            node, observed = row['node'], row['observation'] or {}
            plan = plans[row['planId']]['contract']
            member = next(m for m in plan['members'] if m['memberId'] == node['memberId'])
            verdicts = observed.get('verdicts', {})
            validation = 'failed' if 'fail' in verdicts.values() else 'passed' if all(verdicts.get(r) == 'pass' for r in ('qa','reviewer','controller')) else 'pending'
            blockers = [d for d in node['dependsOn'] if rows[d]['state'] != 'done' or rows[d]['cancelRequested']]
            state = row['state']
            next_action = ('核对执行事实，禁止自动重派' if state == 'unknown' else
                           '等待宿主停止证据；已阻止后续派发' if state == 'cancel_requested' else
                           '本计划停止派发；保留已有结果' if row['cancelRequested'] else
                           '完成独立 QA、review 和集成验收' if state == 'awaiting_validation' else
                           '回读 Paperclip 完成标记' if state == 'accepted_pending_sync' else
                           '修复失败原因后另建受控尝试' if state == 'failed' else
                           '等待依赖验收完成' if blockers else '等待运行结果' if state in ACTIVE else
                           '已交付' if state == 'done' else '等待并发空位')
            cards.append({'taskId':task, 'title':node['title'], 'planId':row['planId'], 'revision':row['contract']['revision'],
                          'memberId':node['memberId'], 'host':member['host'], 'role':node['role'], 'state':state,
                          'executionState':observed.get('state','todo'), 'validationState':validation, 'verdicts':verdicts,
                          'syncState':observed.get('syncState','not_synced'), 'issueId':row['contract']['issueId'],
                          'attemptId':row['attemptId'], 'resultDigest':observed.get('resultDigest'),
                          'dependsOn':node['dependsOn'], 'blockers':blockers, 'error':row['error'], 'nextAction':next_action,
                          'cancelRequested':row['cancelRequested'], 'stopConfirmed':row['stopConfirmed']})
        return {'generatedAt':time.time(), 'source':'gateway-and-durable-scheduler', 'quota':'disabled',
                'plans':[{'planId':k,'title':v['contract']['title'],'revision':v['contract']['revision'],'status':v['status']} for k,v in plans.items()], 'cards':cards}

    def close(self, wait=True):
        self.pool.shutdown(wait=wait)
