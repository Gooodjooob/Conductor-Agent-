"""Explicit, trusted plan dispatch. No automatic approvals or Paperclip writes."""
import copy
import fcntl
import hashlib
import os
from pathlib import Path
import secrets
import stat
import tempfile
import threading
import time

from .dashboard import make_dashboard
from .gateway import Gateway, Rejected, digest, fields, identifier, require
from .paperclip_import import ALLOWED_SOURCE_STATES, private_json, write_private_json
from .recovery import state_lease
from .runner import durable_json, run_job
from .scheduler import Scheduler, validate_plan
from .server import make_server

ADMIN = {'id': 'execution-admin', 'role': 'admin', 'tasks': ['*']}
ROLES = ('worker', 'runner', 'qa', 'reviewer', 'controller')
PROVIDERS = {'dsh': 'dsh', 'codex': 'codex', 'workbuddy': 'workbuddy',
             'openclaw': 'openclaw', 'qclaw': 'openclaw'}
COMMON_JOB = {'taskId', 'provider', 'prompt', 'timeout', 'model'}
CLI_JOB = {'command', 'runtimeRoots', 'sandbox'}
OPENCLAW_JOB = {'openclawCommand', 'openclawStateDir', 'openclawConfigPath',
                'openclawAgentId', 'openclawSessionKey', 'openclawReadOnlyPreflight'}


def error_code(exc):
    # Never surface exception text from a provider/command/prompt.
    code = exc.code if isinstance(exc, Rejected) else type(exc).__name__
    return code if isinstance(code, str) and identifier(code) else 'EXECUTION_ERROR'


def overlap(left, right):
    return left == right or left in right.parents or right in left.parents


def _command(value):
    require(isinstance(value, list) and value and all(isinstance(v, str) and '\x00' not in v for v in value),
            'INVALID_JOB_COMMAND', 400)
    path = Path(value[0])
    require(path.is_absolute() and path.is_file() and os.access(path, os.X_OK), 'JOB_EXECUTABLE_NOT_READY', 400)


def validate_execution(document, config, state_dir):
    """Validate the entire bundle before creating live state or dispatching anything."""
    require(isinstance(document, dict) and document.get('schema') == 'paperclip-orchestrator-plan/v1',
            'INVALID_IMPORTED_PLAN_SCHEMA', 400)
    require(isinstance(document.get('paperclip'), dict) and identifier(document['paperclip'].get('issueId')),
            'PAPERCLIP_BINDING_REQUIRED', 400)
    fields(config, 'schema jobs')
    require(config['schema'] == 'orchestrator-jobs/v1', 'INVALID_JOBS_SCHEMA', 400)
    root = Path(state_dir)
    require(root.is_absolute() and str(root.resolve()) == str(root), 'CANONICAL_STATE_DIR_REQUIRED', 400)
    require(isinstance(document.get('tasks'), list) and document['tasks'], 'TASK_SPECS_REQUIRED', 400)
    specs, issues, identities = {}, set(), {}
    for spec in document['tasks']:
        require(isinstance(spec, dict) and identifier(spec.get('taskId')), 'INVALID_TASK_SPEC', 400)
        require(spec['taskId'] not in specs and spec.get('issueId') not in issues, 'DUPLICATE_TASK_OR_ISSUE', 400)
        specs[spec['taskId']] = spec
        issues.add(spec.get('issueId'))
        workspace = Path(spec.get('workspace', ''))
        require(not overlap(workspace.resolve(), root), 'WORKSPACE_STATE_OVERLAP', 400)
        for role in ROLES:
            identity = spec.get(role + 'Id')
            require(identifier(identity) and identity != ADMIN['id'], 'INVALID_TASK_IDENTITY', 400)
            require(identity not in identities or identities[identity] == role, 'CROSS_ROLE_IDENTITY_REUSE', 400)
            identities[identity] = role

    # Validate every full Gateway contract (including child ordering), not just
    # the subset needed by the Scheduler. The temporary DB has no live state.
    order, visiting, visited = [], set(), set()
    def visit(task_id):
        require(task_id in specs, 'MISSING_CHILD_TASK', 400)
        require(task_id not in visiting, 'DEPENDENCY_CYCLE', 400)
        if task_id in visited:
            return
        visiting.add(task_id)
        children = specs[task_id].get('children')
        require(isinstance(children, list) and all(identifier(c) for c in children), 'INVALID_CHILDREN', 400)
        for child in children:
            visit(child)
        visiting.remove(task_id)
        visited.add(task_id)
        order.append(task_id)
    for task_id in specs:
        visit(task_id)
    with tempfile.TemporaryDirectory(prefix='orchestrator-contract-check-') as directory:
        gateway = Gateway(str(Path(directory).resolve()))
        for task_id in order:
            gateway.create(ADMIN, specs[task_id])
        plan_contracts = validate_plan(document.get('plan'), lambda t: gateway.get(ADMIN, t))
    require(set(specs) == set(plan_contracts), 'PLAN_TASK_SPEC_MISMATCH', 400)

    nodes = {node['taskId']: node for node in document['plan']['nodes']}
    members = {m['memberId']: m for m in document['plan']['members']}
    require(isinstance(config['jobs'], list), 'INVALID_JOBS', 400)
    jobs, sessions = {}, set()
    for job in config['jobs']:
        require(isinstance(job, dict), 'INVALID_JOB', 400)
        task_id, provider = job.get('taskId'), job.get('provider')
        require(identifier(task_id) and task_id in specs and task_id not in jobs, 'JOB_TASK_BINDING_MISMATCH', 400)
        host = members[nodes[task_id]['memberId']]['host']
        require(host in PROVIDERS and provider == PROVIDERS[host], 'JOB_HOST_PROVIDER_MISMATCH', 400)
        allowed = COMMON_JOB | (OPENCLAW_JOB if provider == 'openclaw' else CLI_JOB)
        require(set(job) <= allowed, 'INVALID_JOB_FIELDS', 400)
        prompt = job.get('prompt')
        require(isinstance(prompt, str) and 0 < len(prompt.encode()) <= 200000, 'INVALID_PROMPT', 400)
        require(hashlib.sha256(prompt.encode()).hexdigest() == specs[task_id]['promptDigest'], 'PROMPT_CONTRACT_MISMATCH', 400)
        require(type(job.get('timeout')) in (int, float) and 0 < job['timeout'] <= 3600, 'INVALID_TIMEOUT', 400)
        if 'model' in job:
            require(isinstance(job['model'], str) and 0 < len(job['model']) <= 256, 'INVALID_MODEL', 400)
        if provider == 'openclaw':
            _command(job.get('openclawCommand'))
            state = Path(job.get('openclawStateDir', ''))
            require(state.is_absolute() and state.is_dir() and not state.is_symlink()
                    and state.stat().st_uid == os.getuid() and state.stat().st_mode & 0o077 == 0,
                    'OPENCLAW_STATE_DIR_MUST_BE_PRIVATE', 400)
            require(not overlap(root, state.resolve()), 'OPENCLAW_STATE_DIR_MUST_BE_DISJOINT', 400)
            require(identifier(job.get('openclawAgentId')), 'INVALID_OPENCLAW_IDENTITY', 400)
            session = job.get('openclawSessionKey')
            require(isinstance(session, str) and session.startswith('agent:' + job['openclawAgentId'] + ':')
                    and 1 <= len(session) <= 256 and not any(c in session for c in ('\n', '\r', '\x00')),
                    'INVALID_OPENCLAW_SESSION', 400)
            session_binding = (str(state.resolve()), session)
            require(session_binding not in sessions, 'OPENCLAW_SESSION_REUSED', 400)
            sessions.add(session_binding)
            require(job.get('openclawReadOnlyPreflight') is True, 'OPENCLAW_READ_ONLY_PREFLIGHT_REQUIRED', 400)
            if 'openclawConfigPath' in job:
                path = Path(job['openclawConfigPath'])
                require(path.is_absolute() and path.is_file() and not path.is_symlink(), 'OPENCLAW_CONFIG_NOT_READY', 400)
        else:
            _command(job.get('command'))
            roots = job.get('runtimeRoots')
            require(isinstance(roots, list) and roots and all(isinstance(p, str) for p in roots), 'RUNTIME_ROOTS_REQUIRED', 400)
            for item in roots:
                path = Path(item)
                require(path.is_absolute() and path.is_dir(), 'RUNTIME_ROOT_NOT_READY', 400)
                require(not overlap(path.resolve(), root), 'RUNTIME_CONTROL_OVERLAP', 400)
                require(all(not overlap(path.resolve(), Path(s['workspace']).resolve()) for s in specs.values()),
                        'RUNTIME_WORKSPACE_OVERLAP', 400)
            require(job.get('sandbox', 'read-only') in ('read-only', 'workspace-write'), 'INVALID_SANDBOX', 400)
        jobs[task_id] = copy.deepcopy(job)
        if provider != 'openclaw':
            jobs[task_id].setdefault('sandbox', 'read-only')
    require(set(jobs) == set(specs), 'JOBS_MUST_COVER_PLAN', 400)
    return specs, jobs, order


def verify_upstream(client, document, task_ids=None):
    """Read-only root/child binding check before any dispatch; never PATCH."""
    client.assert_boundary()
    source = document['paperclip']
    root_id = source['issueId']
    code, root = client.request('GET', '/issues/' + root_id)
    require(code == 200 and isinstance(root, dict) and root.get('id') == root_id, 'PAPERCLIP_ROOT_READ_FAILED')
    require(root.get('status') in ALLOWED_SOURCE_STATES, 'PAPERCLIP_ROOT_NOT_DISPATCHABLE')
    for key in ('title', 'description', 'projectId', 'parentId'):
        default = '' if key == 'description' else None
        require(root.get(key, default) == source.get(key, default), 'PAPERCLIP_ROOT_DRIFT')
    for spec in document['tasks']:
        if task_ids is not None and spec['taskId'] not in task_ids:
            continue
        if spec['issueId'] == root_id:
            issue = root
        else:
            code, issue = client.request('GET', '/issues/' + spec['issueId'])
            require(code == 200 and isinstance(issue, dict) and issue.get('id') == spec['issueId'], 'PAPERCLIP_TASK_READ_FAILED')
            require(issue.get('parentId') == root_id, 'PAPERCLIP_CHILD_BINDING_MISMATCH')
            require(issue.get('companyId') == root.get('companyId') and issue.get('projectId') == root.get('projectId'),
                    'PAPERCLIP_PROJECT_MISMATCH')
        require(issue.get('status') in ALLOWED_SOURCE_STATES, 'PAPERCLIP_TASK_NOT_DISPATCHABLE')
        require(all(issue.get(k) == v for k, v in spec['upstreamPolicy'].items()), 'UPSTREAM_POLICY_DRIFT')


class PlanExecution:
    """One process owns a durable plan; existing launch intents are never replayed."""
    def __init__(self, state_dir, document, jobs_config, paperclip, gateway_port=0, dashboard_port=0, resume=False):
        self.root = Path(state_dir)
        self.document = copy.deepcopy(document)
        self.paperclip = paperclip
        self.specs, self.jobs, self.order = validate_execution(self.document, jobs_config, self.root)
        self.mode = 'plan-execute'
        self.gateway = self.dashboard = self.scheduler = None
        self.gateway_thread = self.dashboard_thread = self.scheduler_thread = None
        self.owner_fd = None
        self.stop_event = threading.Event()
        self.closed = False
        self.started = False
        self.last_observed = None
        self.scheduler_error = None
        self.health_lock = threading.RLock()
        self.gateway_port, self.dashboard_port = gateway_port, dashboard_port
        # Resume can observe an upstream-completed plan. It checks freshness
        # again before each still-queued node, rather than re-running results.
        if not resume:
            verify_upstream(paperclip, document)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not self.root.is_symlink() and self.root.stat().st_uid == os.getuid()
                and self.root.stat().st_mode & 0o077 == 0, 'LOCAL_MVP_STATE_MUST_BE_PRIVATE')
        entries = list(self.root.iterdir())
        require(resume or not entries, 'EXECUTION_STATE_EXISTS_USE_RESUME')
        binding = {'schema': 'orchestrator-execution/v1', 'root': str(self.root),
                   'document': self.document, 'jobs': jobs_config, 'paperclipUrl': paperclip.url}
        try:
            self.owner_fd = os.open(str(self.root / 'execution.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            info = os.fstat(self.owner_fd)
            require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.getuid()
                    and info.st_mode & 0o077 == 0, 'UNSAFE_EXECUTION_LOCK')
            try:
                fcntl.flock(self.owner_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Rejected('EXECUTION_ALREADY_RUNNING')
            require(not (self.root / 'RESTORE_PENDING.json').exists(), 'RESTORE_ACTIVATION_REQUIRED')
            path = self.root / 'execution.json'
            if resume:
                require(private_json(path) == binding, 'EXECUTION_BINDING_CHANGED')
                require(all((self.root / name).is_file() and not (self.root / name).is_symlink()
                            for name in ('gateway.sqlite', 'scheduler.sqlite', 'access.json')),
                        'EXECUTION_LEDGER_MISSING_NO_REPLAY')
            else:
                write_private_json(path, binding)
            self.credentials_path = self.root / 'access.json'
            identities = {}
            for task_id, spec in self.specs.items():
                for role in ROLES:
                    ident = spec[role + 'Id']
                    identities.setdefault(ident, {'id': ident, 'role': role, 'tasks': []})['tasks'].append(task_id)
            if resume:
                access = private_json(self.credentials_path)
                require(set(access) == set(identities), 'EXECUTION_CREDENTIAL_BINDING_CHANGED')
            else:
                access = {ident: secrets.token_urlsafe(32) for ident in identities}
                write_private_json(self.credentials_path, access)
            principals = []
            for ident, actor in identities.items():
                token = access[ident]
                require(isinstance(token, str) and 32 <= len(token) <= 512, 'INVALID_EXECUTION_TOKEN')
                principal = dict(actor, tokenSha256=hashlib.sha256(token.encode()).hexdigest())
                if actor['role'] in ('qa', 'reviewer', 'controller'):
                    (self.root / 'evidence').mkdir(mode=0o700, exist_ok=True)
                    evidence = self.root / 'evidence' / ident
                    evidence.mkdir(mode=0o700, parents=True, exist_ok=True)
                    principal['evidenceRoot'] = str(evidence)
                principals.append(principal)
            self.runner_tokens = {t: access[s['runnerId']] for t, s in self.specs.items()}
            # Explicitly no upstream write client. Approval must be independently
            # supplied; finalization cannot fabricate a synced dependency.
            config = {'stateDir': str(self.root), 'principals': principals, 'paperclip': None}
            self.gateway = make_server(config, gateway_port)
            for task_id in self.order:
                self.gateway.gateway.create(ADMIN, self.specs[task_id])
            self.scheduler = Scheduler(str(self.root), self.read_task, self.launch,
                                       max_concurrency=document['plan']['maxConcurrency'])
            self.scheduler.submit_plan(document['plan'])
            self.dashboard_token = secrets.token_urlsafe(32)
            self.dashboard = make_dashboard(self.snapshot, self.dashboard_token, dashboard_port)
        except BaseException:
            self.close()
            raise

    def read_task(self, task_id):
        result = self.gateway.gateway.get(ADMIN, task_id)
        error_path = self.root / ('launch-error-' + digest(task_id) + '.json')
        if error_path.exists():
            result['dispatchError'] = private_json(error_path)['error']
        return result

    def launch(self, node, _member, attempt_id):
        with state_lease(self.root):
            return self._launch(node, attempt_id)

    def _launch(self, node, attempt_id):
        task_id = node['taskId']
        try:
            require(not self.stop_event.is_set(), 'DISPATCH_STOPPED_BEFORE_SUBMIT')
            verify_upstream(self.paperclip, self.document, {task_id})
            require(not self.stop_event.is_set(), 'DISPATCH_STOPPED_BEFORE_SUBMIT')
            job = dict(self.jobs[task_id], stateDir=str(self.root), attemptId=attempt_id,
                       gatewayUrl=self.addresses['gateway'], gatewayToken=self.runner_tokens[task_id])
            path = self.root / ('runner-' + digest(task_id) + '.json')
            durable_json(path, job)
            return run_job(str(path))
        except Exception as exc:
            durable_json(self.root / ('launch-error-' + digest(task_id) + '.json'),
                         {'error': error_code(exc), 'attemptId': attempt_id})
            raise

    def _tick(self, dispatch=True):
        self.scheduler.tick(dispatch=dispatch)
        with self.health_lock:
            self.last_observed = time.time()

    def _loop(self):
        while not self.stop_event.is_set():
            try:
                self._tick()
            except Exception as exc:
                with self.health_lock:
                    self.scheduler_error = error_code(exc)
                self.stop_event.set()  # Show the error; never silently continue.
                return
            self.stop_event.wait(0.5)

    def start(self):
        require(not self.started and not self.closed, 'EXECUTION_NOT_STARTABLE')
        self.started = True
        try:
            self.gateway_thread = threading.Thread(target=self.gateway.serve_forever, name='execute-gateway', daemon=True)
            self.dashboard_thread = threading.Thread(target=self.dashboard.serve_forever, name='execute-dashboard', daemon=True)
            self.gateway_thread.start()
            self.dashboard_thread.start()
            self.scheduler_thread = threading.Thread(target=self._loop, name='execute-scheduler', daemon=True)
            self.scheduler_thread.start()
            return self
        except BaseException:
            self.close()
            raise

    def snapshot(self):
        result = self.scheduler.snapshot()
        with self.health_lock:
            result.update(live=True, mode=self.mode, evidenceType='gateway/runner-receipts',
                          lastObservedAt=self.last_observed, schedulerError=self.scheduler_error,
                          dispatchEnabled=not self.stop_event.is_set(), writeBack=False)
        result['paperclip'] = {k: self.document['paperclip'].get(k) for k in ('issueId', 'title', 'status', 'projectId')}
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop_event.set()
        if self.scheduler_thread:
            self.scheduler_thread.join()
        # Keep Gateway alive until in-flight runners deliver or reach their
        # configured timeout. Shutdown does not claim remote cancellation.
        try:
            if self.scheduler:
                self.scheduler.close(wait=True)
                if self.started:
                    self._tick(dispatch=False)
        finally:
            for server, thread in ((self.dashboard, self.dashboard_thread), (self.gateway, self.gateway_thread)):
                if server:
                    if thread and thread.is_alive():
                        server.shutdown()
                        thread.join()
                    server.server_close()
            if self.owner_fd is not None:
                os.close(self.owner_fd)
                self.owner_fd = None

    @property
    def addresses(self):
        return {'gateway': 'http://127.0.0.1:' + str(self.gateway.server_port),
                'dashboard': 'http://127.0.0.1:' + str(self.dashboard.server_port)}
