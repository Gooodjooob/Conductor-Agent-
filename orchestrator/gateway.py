"""Durable receipt/approval boundary; Paperclip remains the ticket authority."""
import hashlib
import json
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from pathlib import Path


class Rejected(Exception):
    def __init__(self, code, status=409):
        super().__init__(code)
        self.code, self.status = code, status


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def require(condition, code, status=409):
    if not condition:
        raise Rejected(code, status)


def fields(body, names):
    require(isinstance(body, dict) and set(body) == set(names.split()), 'INVALID_FIELDS', 400)


def identifier(value):
    return isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', value)


def sha(value):
    return isinstance(value, str) and re.fullmatch(r'[0-9a-f]{64}', value)


def manifest(root, items, snapshot=None):
    """Read via directory fds so a symlink cannot escape the granted root."""
    require(isinstance(items, list) and 0 < len(items) <= 64, 'ARTIFACTS_REQUIRED', 400)
    require(Path(root).is_absolute(), 'INVALID_ROOT', 400)
    # Every root component is checked with O_NOFOLLOW, including the root itself.
    root_fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in Path(root).parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
            os.close(root_fd)
            root_fd = child
        result, seen = [], set()
        for item in items:
            fields(item, 'path sha256 size')
            name = item['path']
            require(isinstance(name, str) and len(name) <= 1024, 'INVALID_ARTIFACT_PATH', 400)
            parts = name.split('/')
            require(not name.startswith('/') and all(p not in ('', '.', '..') for p in parts), 'INVALID_ARTIFACT_PATH', 400)
            require(name not in seen and sha(item['sha256']) and type(item['size']) is int and 0 <= item['size'] <= 20_000_000, 'INVALID_ARTIFACT', 400)
            seen.add(name)
            current = os.dup(root_fd)
            try:
                for component in parts[:-1]:
                    child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
                    os.close(current)
                    current = child
                fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
                with os.fdopen(fd, 'rb') as stream:
                    before = os.fstat(stream.fileno())
                    require(stat.S_ISREG(before.st_mode) and before.st_nlink == 1, 'UNSAFE_ARTIFACT')
                    require(before.st_size == item['size'], 'ARTIFACT_CHANGED')
                    data = stream.read(20_000_001)
                    after = os.fstat(stream.fileno())
                    require((before.st_size, before.st_mtime_ns, before.st_ctime_ns) == (after.st_size, after.st_mtime_ns, after.st_ctime_ns), 'ARTIFACT_CHANGED')
                    require(len(data) == item['size'] and hashlib.sha256(data).hexdigest() == item['sha256'], 'ARTIFACT_CHANGED')
                    if snapshot is not None:
                        target = Path(snapshot) / item['sha256']
                        try:
                            output = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o400)
                        except FileExistsError:
                            require(not target.is_symlink() and hashlib.sha256(target.read_bytes()).hexdigest() == item['sha256'], 'SNAPSHOT_CORRUPTED')
                        else:
                            with os.fdopen(output, 'wb') as stream:
                                stream.write(data)
                                stream.flush()
                                os.fsync(stream.fileno())
                result.append(dict(item))
            finally:
                os.close(current)
        return sorted(result, key=lambda entry: entry['path'])
    except OSError as exc:
        raise Rejected('ARTIFACT_UNREADABLE') from exc
    finally:
        os.close(root_fd)


class Gateway:
    def __init__(self, state_dir):
        path = Path(state_dir)
        require(path.is_absolute() and not path.is_symlink(), 'INVALID_STATE_DIR', 400)
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(path.stat().st_uid == os.getuid() and path.stat().st_mode & 0o077 == 0, 'INSECURE_STATE_DIR', 400)
        self.db = str(path / 'gateway.sqlite')
        self.snapshots = path / 'artifacts'
        require(not self.snapshots.is_symlink(), 'INVALID_SNAPSHOT_DIR')
        self.snapshots.mkdir(mode=0o700, exist_ok=True)
        require(not Path(self.db).is_symlink(), 'INVALID_DATABASE', 400)
        with self.transaction() as db:
            db.executescript('''CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attempts (id TEXT PRIMARY KEY, task TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY, actor TEXT, action TEXT, task TEXT, digest TEXT);
                CREATE TABLE IF NOT EXISTS outbox (task TEXT PRIMARY KEY, state TEXT NOT NULL, expected TEXT NOT NULL, error TEXT);''')
        os.chmod(self.db, 0o600)

    @contextmanager
    def transaction(self):
        from .recovery import state_lease
        with state_lease(Path(self.db).parent):
            with self._transaction_locked() as db:
                yield db

    @contextmanager
    def _transaction_locked(self):
        db = sqlite3.connect(self.db, timeout=10, isolation_level=None)
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def authorize(self, actor, roles, task=None):
        require(actor.get('role') in roles and identifier(actor.get('id')), 'FORBIDDEN', 403)
        require(task is None or actor.get('tasks') == ['*'] or task in actor.get('tasks', []), 'OUT_OF_SCOPE', 403)

    def load(self, db, task):
        row = db.execute('SELECT body FROM tasks WHERE id=?', (task,)).fetchone()
        require(row is not None, 'TASK_NOT_FOUND', 404)
        return json.loads(row[0])

    def save(self, db, task, actor, action):
        db.execute('UPDATE tasks SET body=? WHERE id=?', (canonical(task), task['spec']['taskId']))
        db.execute('INSERT INTO audit(actor,action,task,digest) VALUES (?,?,?,?)', (actor['id'], action, task['spec']['taskId'], digest(task)))

    def create(self, actor, spec):
        self.authorize(actor, ['admin'], spec.get('taskId'))
        fields(spec, 'taskId revision issueId workerId runnerId qaId reviewerId controllerId workspace instructionDigest promptDigest artifactPaths children upstreamPolicy')
        for key in ('taskId', 'issueId', 'workerId', 'runnerId', 'qaId', 'reviewerId', 'controllerId'):
            require(identifier(spec[key]), 'INVALID_ID', 400)
        require(type(spec['revision']) is int and spec['revision'] > 0 and sha(spec['instructionDigest']) and sha(spec['promptDigest']), 'INVALID_CONTRACT', 400)
        paths = spec['artifactPaths']
        require(isinstance(paths, list) and 0 < len(paths) <= 64 and all(isinstance(p, str) and len(p) <= 1024 and all(part not in ('', '.', '..') for part in p.split('/')) for p in paths), 'INVALID_ARTIFACT_PATHS', 400)
        require(paths == sorted(set(paths)), 'CANONICAL_PATHS_REQUIRED', 400)
        require(len({spec[k] for k in ('workerId', 'runnerId', 'qaId', 'reviewerId', 'controllerId')}) == 5, 'IDENTITIES_MUST_BE_DISTINCT', 400)
        root = Path(spec['workspace'])
        require(root.is_absolute() and root.is_dir() and str(root.resolve()) == str(root), 'INVALID_WORKSPACE', 400)
        state_root = Path(self.db).parent.resolve()
        require(root != state_root and root not in state_root.parents and state_root not in root.parents, 'WORKSPACE_STATE_OVERLAP', 400)
        fields(spec['upstreamPolicy'], 'reviewPolicy executionPolicy parentId')
        require(isinstance(spec['children'], list) and len(spec['children']) == len(set(spec['children'])) and all(identifier(c) for c in spec['children']), 'INVALID_CHILDREN', 400)
        with self.transaction() as db:
            row = db.execute('SELECT body FROM tasks WHERE id=?', (spec['taskId'],)).fetchone()
            if row:
                old = json.loads(row[0])
                require(old['spec'] == spec, 'IMMUTABLE_CONTRACT')
                return self.public(old)
            require(not any(json.loads(r[0])['spec']['issueId'] == spec['issueId'] for r in db.execute('SELECT body FROM tasks')), 'ISSUE_ALREADY_BOUND')
            for child in spec['children']:
                self.load(db, child)  # Children pre-exist; immutable edges cannot form cycles.
            task = {'spec': spec, 'receipt': None, 'verdicts': {}, 'accepted': False, 'attemptHistory': [], 'requested': False}
            db.execute('INSERT INTO tasks VALUES (?,?)', (spec['taskId'], canonical(task)))
            self.save(db, task, actor, 'create')
            return self.public(task)

    def public(self, task):
        return {'taskId': task['spec']['taskId'], 'revision': task['spec']['revision'], 'issueId': task['spec']['issueId'],
                'state': 'accepted_pending_sync' if task['accepted'] else (task['receipt'] or {}).get('state', 'todo'),
                'attemptId': (task['receipt'] or {}).get('attemptId'), 'resultDigest': task.get('resultDigest'),
                'verdicts': {k: v['decision'] for k, v in task['verdicts'].items()}, 'completionRequested': task['requested']}

    def get(self, actor, task_id):
        self.authorize(actor, ['admin', 'worker', 'runner', 'qa', 'reviewer', 'controller'], task_id)
        with self.transaction() as db:
            task = self.load(db, task_id)
            role = actor['role']
            if role != 'admin':
                require(task['spec'][role + 'Id'] == actor['id'], 'IDENTITY_MISMATCH', 403)
            result = self.public(task)
            if role in ('admin', 'runner', 'controller'):
                result['contract'] = task['spec']
            out = db.execute('SELECT state,error FROM outbox WHERE task=?', (task_id,)).fetchone()
            if out:
                result['syncState'], result['syncError'] = out
                if out[0] == 'synced':
                    result['state'] = 'done'
            return result

    def completion_request(self, actor, task_id):
        self.authorize(actor, ['worker'], task_id)
        with self.transaction() as db:
            task = self.load(db, task_id)
            require(task['spec']['workerId'] == actor['id'], 'IDENTITY_MISMATCH', 403)
            require(not task['accepted'], 'TASK_FROZEN')
            task['requested'] = True
            self.save(db, task, actor, 'completion_request')
            return self.public(task)

    def claim(self, actor, body):
        """One durable dispatch permit, consumed before any SDK process starts."""
        fields(body, 'taskId revision attemptId instructionDigest')
        self.authorize(actor, ['runner'], body['taskId'])
        require(type(body['revision']) is int and identifier(body['attemptId']), 'INVALID_CLAIM', 400)
        with self.transaction() as db:
            task = self.load(db, body['taskId']); spec = task['spec']
            require(actor['id'] == spec['runnerId'], 'IDENTITY_MISMATCH', 403)
            require(body['revision'] == spec['revision'] and body['instructionDigest'] == spec['instructionDigest'], 'CONTRACT_MISMATCH')
            require(not task['accepted'], 'TASK_FROZEN')
            if task['receipt']:
                require(task['receipt']['attemptId'] == body['attemptId'], 'EXPLICIT_RETRY_REQUIRED')
                return {'canSubmit': False, 'attemptId': body['attemptId'], 'state': task['receipt']['state']}
            require(db.execute('SELECT 1 FROM attempts WHERE id=?', (body['attemptId'],)).fetchone() is None, 'ATTEMPT_REUSED')
            db.execute('INSERT INTO attempts VALUES (?,?)', (body['attemptId'], body['taskId']))
            receipt = dict(body, state='unknown', sessionId=None, messageId=None, artifacts=[])
            task.update(receipt=receipt, claimPending=True, resultDigest=digest(receipt))
            self.save(db, task, actor, 'claim_before_send')
            return {'canSubmit': True, 'attemptId': body['attemptId'], 'state': 'unknown'}

    def receipt(self, actor, body):
        fields(body, 'taskId revision attemptId instructionDigest state sessionId messageId artifacts')
        require(type(body['revision']) is int and body['revision'] > 0, 'INVALID_REVISION', 400)
        self.authorize(actor, ['runner'], body['taskId'])
        require(identifier(body['attemptId']) and identifier(body['sessionId']), 'INVALID_RECEIPT', 400)
        require(body['messageId'] is None or identifier(body['messageId']), 'INVALID_RECEIPT', 400)
        require(body['state'] in ('accepted', 'running', 'failed', 'unknown', 'awaiting_validation'), 'INVALID_STATE', 400)
        with self.transaction() as db:
            task = self.load(db, body['taskId']); spec = task['spec']
            require(actor['id'] == spec['runnerId'], 'IDENTITY_MISMATCH', 403)
            if task['accepted'] and task['receipt'] == body:
                return self.public(task)
            require(not task['accepted'], 'TASK_FROZEN')
            require(body['revision'] == spec['revision'] and body['instructionDigest'] == spec['instructionDigest'], 'CONTRACT_MISMATCH')
            old = task['receipt']
            if old:
                require(old['attemptId'] == body['attemptId'] and (old['sessionId'] is None or old['sessionId'] == body['sessionId']), 'EXPLICIT_RETRY_REQUIRED')
                require(old['messageId'] is None or old['messageId'] == body['messageId'], 'MESSAGE_MISMATCH')
                if old['state'] in ('failed', 'awaiting_validation'):
                    require(old == body, 'TERMINAL_RECEIPT_IMMUTABLE')
                    return self.public(task)
                require(task.get('claimPending') or not(old['state'] in ('running', 'unknown') and body['state'] == 'accepted'), 'STATE_REGRESSION')
            else:
                require(db.execute('SELECT 1 FROM attempts WHERE id=?', (body['attemptId'],)).fetchone() is None, 'ATTEMPT_REUSED')
                db.execute('INSERT INTO attempts VALUES (?,?)', (body['attemptId'], body['taskId']))
            if body['state'] == 'awaiting_validation':
                require(body['messageId'] is not None, 'ACCEPTANCE_EVIDENCE_REQUIRED')
                require(manifest(spec['workspace'], body['artifacts'], self.snapshots) == body['artifacts'], 'CANONICAL_MANIFEST_REQUIRED')
                require([item['path'] for item in body['artifacts']] == spec['artifactPaths'], 'ARTIFACT_SCOPE_MISMATCH')
            else:
                require(body['artifacts'] == [], 'UNVERIFIED_ARTIFACTS')
            task.update(receipt=body, verdicts={}, requested=False, claimPending=False, resultDigest=digest(body))
            self.save(db, task, actor, 'receipt')
            return self.public(task)

    def retry(self, actor, task_id):
        self.authorize(actor, ['admin'], task_id)
        with self.transaction() as db:
            task = self.load(db, task_id)
            require(not task['accepted'] and task['receipt'] and task['receipt']['state'] == 'failed', 'RETRY_UNSAFE')
            task['attemptHistory'].append(task['receipt'])
            task.update(receipt=None, verdicts={}, resultDigest=None, requested=False)
            self.save(db, task, actor, 'explicit_retry')
            return self.public(task)

    def verify_result(self, task):
        require(task['receipt'] and task['receipt']['state'] == 'awaiting_validation', 'RESULT_NOT_READY')
        manifest(task['spec']['workspace'], task['receipt']['artifacts'])
        for artifact in task['receipt']['artifacts']:
            manifest(str(self.snapshots), [{'path': artifact['sha256'], 'sha256': artifact['sha256'], 'size': artifact['size']}])

    def verdict(self, actor, body):
        fields(body, 'taskId revision attemptId resultDigest decision evidence')
        require(type(body['revision']) is int and body['revision'] > 0, 'INVALID_REVISION', 400)
        self.authorize(actor, ['qa', 'reviewer', 'controller'], body['taskId'])
        require(body['decision'] in ('pass', 'fail'), 'INVALID_VERDICT', 400)
        with self.transaction() as db:
            task = self.load(db, body['taskId']); spec = task['spec']
            require(actor['id'] == spec[actor['role'] + 'Id'], 'IDENTITY_MISMATCH', 403)
            previous = task['verdicts'].get(actor['role'])
            if task['accepted'] and previous and previous['body'] == body and previous['actor'] == actor['id']:
                return self.public(task)
            require(not task['accepted'], 'TASK_FROZEN')
            self.verify_result(task)
            require(body['revision'] == spec['revision'] and body['attemptId'] == task['receipt']['attemptId'] and body['resultDigest'] == task['resultDigest'], 'STALE_VERDICT')
            evidence_root = actor.get('evidenceRoot')
            require(isinstance(evidence_root, str), 'EVIDENCE_ROOT_REQUIRED')
            evidence_path, workspace_path = Path(evidence_root).resolve(), Path(spec['workspace']).resolve()
            require(workspace_path not in (evidence_path, *evidence_path.parents) and evidence_path not in workspace_path.parents, 'EVIDENCE_NOT_INDEPENDENT')
            manifest(evidence_root, body['evidence'])
            previous = task['verdicts'].get(actor['role'])
            if previous:
                require(previous['body'] == body and previous['actor'] == actor['id'], 'VERDICT_IMMUTABLE')
            task['verdicts'][actor['role']] = {'actor': actor['id'], 'decision': body['decision'], 'root': evidence_root, 'body': body}
            self.save(db, task, actor, 'verdict')
            return self.public(task)

    def verify_gate(self, db, task, seen=None):
        seen = set() if seen is None else seen
        require(task['spec']['taskId'] not in seen, 'DEPENDENCY_CYCLE')
        seen.add(task['spec']['taskId'])
        self.verify_result(task)
        for role in ('qa', 'reviewer', 'controller'):
            approval = task['verdicts'].get(role)
            require(approval is not None and approval['decision'] == 'pass', 'MISSING_' + role.upper())
            require(approval['body']['resultDigest'] == task['resultDigest'], 'STALE_VERDICT')
            manifest(approval['root'], approval['body']['evidence'])
        for child_id in task['spec']['children']:
            child = self.load(db, child_id)
            out = db.execute('SELECT state FROM outbox WHERE task=?', (child_id,)).fetchone()
            require(child['accepted'] and out and out[0] == 'synced', 'CHILD_NOT_DONE')
            self.verify_gate(db, child, set(seen))

    def finalize(self, actor, task_id):
        self.authorize(actor, ['controller'], task_id)
        with self.transaction() as db:
            task = self.load(db, task_id)
            require(actor['id'] == task['spec']['controllerId'], 'IDENTITY_MISMATCH', 403)
            self.verify_gate(db, task)
            task['accepted'] = True
            expected = {'issueId': task['spec']['issueId'], 'resultDigest': task['resultDigest'], 'policy': task['spec']['upstreamPolicy']}
            expected['children'] = []
            for child_id in task['spec']['children']:
                child = self.load(db, child_id)
                expected['children'].append({'issueId': child['spec']['issueId'], 'resultDigest': child['resultDigest'], 'policy': child['spec']['upstreamPolicy']})
            db.execute("INSERT OR IGNORE INTO outbox VALUES (?,'pending',?,NULL)", (task_id, canonical(expected)))
            self.save(db, task, actor, 'finalize')
            return self.public(task)

    def flush(self, actor, task_id, client):
        """Serialize gate revalidation and reconciliation; never rerun an agent."""
        self.authorize(actor, ['controller'], task_id)
        with self.transaction() as db:
            task = self.load(db, task_id)
            require(actor['id'] == task['spec']['controllerId'], 'IDENTITY_MISMATCH', 403)
            require(task['accepted'], 'NOT_FINALIZED')
            self.verify_gate(db, task)
            row = db.execute('SELECT expected FROM outbox WHERE task=?', (task_id,)).fetchone()
            require(row is not None, 'OUTBOX_MISSING')
            expected = json.loads(row[0])
            try:
                status = client.reconcile_and_close(expected)
                require(status == 'synced', 'INVALID_SYNC_RESPONSE')
                db.execute("UPDATE outbox SET state='synced',error=NULL WHERE task=?", (task_id,))
            except Exception as exc:
                code = exc.code if isinstance(exc, Rejected) else 'UPSTREAM_UNKNOWN'
                db.execute("UPDATE outbox SET state='sync_unknown',error=? WHERE task=?", (code, task_id))
                status = 'sync_unknown'
            self.save(db, task, actor, 'sync_' + status)
            return {'taskId': task_id, 'syncState': status}
