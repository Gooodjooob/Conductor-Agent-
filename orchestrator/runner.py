"""Trusted offline runner: credentials stay here, never in the worker env."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from types import SimpleNamespace
import urllib.error
import urllib.parse
import urllib.request
from .dsh import DSHAdapter, AdapterError
from .codex import CodexAdapter
from .workbuddy import WorkBuddyAdapter
from .openclaw import OpenClawAdapter, OpenClawGatewayTransport
from .gateway import Rejected, canonical, digest, fields, identifier, require
from .isolation import prepare_launch
from .paperclip import NoRedirect


RECEIPT_FIELDS = 'taskId revision attemptId instructionDigest state sessionId messageId artifacts'.split()


def private_json(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_mode & 0o077 == 0, 'CONFIG_MUST_BE_PRIVATE', 400)
        return json.load(stream)


def durable_json(path, value):
    fd, name = tempfile.mkstemp(dir=str(path.parent), prefix='.receipt-')
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(canonical(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, str(path))
        parent = os.open(str(path.parent), os.O_RDONLY)
        try: os.fsync(parent)
        finally: os.close(parent)
    finally:
        if os.path.exists(name): os.unlink(name)


class GatewayClient:
    def __init__(self, url, token):
        parsed = urllib.parse.urlsplit(url)
        require(parsed.scheme == 'http' and parsed.hostname == '127.0.0.1' and not parsed.username and not parsed.query and not parsed.fragment and parsed.path in ('', '/'), 'LOOPBACK_GATEWAY_REQUIRED', 400)
        require(isinstance(token, str) and 24 <= len(token) <= 512 and '\n' not in token and '\r' not in token, 'INVALID_GATEWAY_TOKEN', 400)
        self.url, self.token = url.rstrip('/'), token
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, body=None):
        request = urllib.request.Request(self.url + path, data=canonical(body).encode() if body is not None else None,
                    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.token}, method='POST' if body is not None else 'GET')
        try:
            with self.opener.open(request, timeout=5) as response:
                raw = response.read(262145)
                require(len(raw) <= 262144, 'GATEWAY_RESPONSE_TOO_LARGE')
                return json.loads(raw)
        except urllib.error.HTTPError as exc:
            # Do not echo arbitrary upstream bodies, prompt or auth headers.
            raise Rejected('GATEWAY_HTTP_' + str(exc.code)) from exc
        except (OSError, ValueError) as exc:
            raise Rejected('GATEWAY_UNREACHABLE') from exc


def _run_job(config_path):
    config = private_json(config_path)
    allowed = {'stateDir','gatewayUrl','gatewayToken','taskId','attemptId','prompt','command','runtimeRoots','timeout','provider','model','sandbox','openclawReadOnlyPreflight',
               'openclawCommand','openclawStateDir','openclawConfigPath','openclawAgentId','openclawSessionKey'}
    require(isinstance(config, dict) and set(config) <= allowed, 'INVALID_JOB_FIELDS', 400)
    required_job = {k: config[k] for k in ('stateDir','gatewayUrl','gatewayToken','taskId','attemptId','prompt','timeout')}
    fields(required_job, 'stateDir gatewayUrl gatewayToken taskId attemptId prompt timeout')
    require(identifier(config['taskId']) and identifier(config['attemptId']), 'INVALID_JOB_ID', 400)
    require(isinstance(config['prompt'], str) and 0 < len(config['prompt']) <= 200000, 'INVALID_PROMPT', 400)
    require(type(config['timeout']) in (float, int) and 0 < config['timeout'] <= 3600, 'INVALID_TIMEOUT', 400)
    state = Path(config['stateDir']).resolve(strict=True)
    require(Path(config_path).resolve().parent == state and state.stat().st_mode & 0o077 == 0, 'RUNNER_CONFIG_MUST_BE_IN_PRIVATE_STATE', 400)
    provider = config.get('provider', 'dsh')
    require(provider in ('dsh', 'codex', 'workbuddy', 'openclaw'), 'UNSUPPORTED_PROVIDER', 400)
    if provider != 'openclaw':
        fields({k: config[k] for k in ('command','runtimeRoots') if k in config}, 'command runtimeRoots')
    else:
        fields({k: config[k] for k in ('openclawCommand','openclawStateDir','openclawAgentId','openclawSessionKey') if k in config},
               'openclawCommand openclawStateDir openclawAgentId openclawSessionKey')
        require(isinstance(config['openclawCommand'], list) and config['openclawCommand'] and
                isinstance(config['openclawCommand'][0], str) and Path(config['openclawCommand'][0]).is_absolute(),
                'ABSOLUTE_OPENCLAW_COMMAND_REQUIRED', 400)
        require(isinstance(config['openclawStateDir'], str) and Path(config['openclawStateDir']).is_absolute(),
                'ABSOLUTE_OPENCLAW_STATE_DIR_REQUIRED', 400)
        openclaw_executable = Path(config['openclawCommand'][0]).resolve(strict=True)
        require(openclaw_executable.is_file() and os.access(str(openclaw_executable), os.X_OK),
                'OPENCLAW_COMMAND_NOT_EXECUTABLE', 400)
        openclaw_state = Path(config['openclawStateDir']).resolve(strict=True)
        require(openclaw_state.is_dir() and openclaw_state.stat().st_mode & 0o077 == 0,
                'OPENCLAW_STATE_DIR_MUST_BE_PRIVATE', 400)
        require(openclaw_state != state and state not in openclaw_state.parents and openclaw_state not in state.parents,
                'OPENCLAW_STATE_DIR_MUST_BE_DISJOINT', 400)
        require(identifier(config['openclawAgentId']) and isinstance(config['openclawSessionKey'], str) and
                1 <= len(config['openclawSessionKey']) <= 256 and '\n' not in config['openclawSessionKey'] and
                '\r' not in config['openclawSessionKey'],
                'INVALID_OPENCLAW_IDENTITY', 400)
        if 'openclawReadOnlyPreflight' in config:
            require(type(config['openclawReadOnlyPreflight']) is bool,
                    'INVALID_OPENCLAW_READ_ONLY_PREFLIGHT', 400)
        if 'openclawConfigPath' in config:
            require(isinstance(config['openclawConfigPath'], str) and Path(config['openclawConfigPath']).is_absolute(),
                    'ABSOLUTE_OPENCLAW_CONFIG_PATH_REQUIRED', 400)
    client = GatewayClient(config['gatewayUrl'], config['gatewayToken'])
    task = client.request('/v1/tasks/' + config['taskId'])
    spec = task.get('contract'); require(spec is not None, 'RUNNER_IDENTITY_REQUIRED', 403)
    require(hashlib.sha256(config['prompt'].encode()).hexdigest() == spec['promptDigest'], 'PROMPT_CONTRACT_MISMATCH')
    directory = state / ('attempt-' + digest(config['attemptId']))
    require(not directory.is_symlink(), 'INVALID_ATTEMPT_STORE')
    directory.mkdir(mode=0o700, exist_ok=True)
    lock = open(directory / 'runner.lock', 'a')
    try:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise Rejected('RUNNER_BUSY')
        frozen = {k: config[k] for k in ('taskId', 'attemptId', 'prompt', 'timeout')}
        if provider != 'openclaw':
            frozen.update({k: config[k] for k in ('command', 'runtimeRoots')})
        else:
            frozen.update({k: config[k] for k in ('openclawCommand','openclawStateDir','openclawAgentId','openclawSessionKey')})
            if 'openclawConfigPath' in config: frozen['openclawConfigPath'] = config['openclawConfigPath']
            if 'openclawReadOnlyPreflight' in config: frozen['openclawReadOnlyPreflight'] = config['openclawReadOnlyPreflight']
        frozen.update({k: config[k] for k in ('provider','model','sandbox') if k in config})
        frozen['contract'] = spec
        binding = directory / 'job.json'
        if binding.exists():
            require(private_json(binding) == frozen, 'JOB_CONTRACT_CONFLICT')
        else:
            durable_json(binding, frozen)
        pending = directory / 'pending.json'
        if not pending.exists():
            claim = client.request('/v1/claims', {'taskId': spec['taskId'], 'revision': spec['revision'],
                                   'attemptId': config['attemptId'], 'instructionDigest': spec['instructionDigest']})
            if provider == 'openclaw':
                transport = OpenClawGatewayTransport(
                    command=config['openclawCommand'],
                    state_dir=config['openclawStateDir'],
                    config_path=config.get('openclawConfigPath'),
                    cwd=spec['workspace'],
                )
                adapter = OpenClawAdapter(
                    directory / 'sdk', spec['workspace'], transport,
                    config['openclawAgentId'], config['openclawSessionKey'],
                    timeout=config['timeout'], execution_timeout=config['timeout'],
                    model=config.get('model'),
                    require_read_only_policy=config.get('openclawReadOnlyPreflight', False),
                )
            else:
                launch_path = directory / 'launch.json'
                if launch_path.exists():
                    launch = SimpleNamespace(**private_json(launch_path))
                else:
                    launch = prepare_launch(config['command'], spec['workspace'], config['runtimeRoots'], str(state))
                    durable_json(launch_path, {'command': launch.command, 'cwd': launch.cwd, 'env': launch.env})
                adapter_cls = {'dsh': DSHAdapter, 'codex': CodexAdapter, 'workbuddy': WorkBuddyAdapter}[provider]
                adapter_kwargs = {'timeout': min(config['timeout'], 10), 'execution_timeout': config['timeout']}
                if provider in ('codex', 'workbuddy'):
                    adapter_kwargs.update(model=config.get('model'), sandbox=config.get('sandbox','workspace-write'))
                adapter = adapter_cls(directory / 'sdk', launch.command, launch.cwd, launch.env, **adapter_kwargs)
            try:
                try:
                    adapter.get_status(config['attemptId'])
                    recovery = True
                except AdapterError as exc:
                    if str(exc) != 'ATTEMPT_NOT_FOUND': raise
                    recovery = False
                if not recovery:
                    require(claim['canSubmit'] is True, 'REMOTE_ATTEMPT_ALREADY_CLAIMED_NO_REPLAY')
                    adapter.start()
                adapter.submit(spec['taskId'], spec['revision'], config['attemptId'], config['prompt'], spec['instructionDigest'])
                deadline = time.monotonic() + config['timeout'] + 1
                while time.monotonic() < deadline:
                    receipt = adapter.get_status(config['attemptId'])
                    if receipt['state'] in ('failed', 'unknown', 'awaiting_validation'): break
                    time.sleep(.05)
                adapter.close()
                receipt = adapter.get_status(config['attemptId'])
                if receipt['state'] == 'awaiting_validation':
                    receipt = adapter.collect(config['attemptId'], spec['artifactPaths'])
                if receipt['state'] not in ('failed', 'unknown', 'awaiting_validation'):
                    receipt = dict(receipt, state='unknown', artifacts=[])
                # Third-party adapters may omit optional artifact metadata on
                # failed/unknown receipts. Normalize the envelope before
                # durable delivery so one malformed optional field cannot
                # crash the trusted runner or strand a terminal attempt.
                receipt = dict(receipt)
                receipt.setdefault('artifacts', [])
                body = {k: receipt[k] for k in RECEIPT_FIELDS}
                durable_json(pending, body)
            finally:
                adapter.close()
        body = private_json(pending)
        status = client.request('/v1/receipts', body)
        durable_json(directory / 'delivered.json', {'receiptDigest': digest(body), 'gateway': status})
        return {'taskId': spec['taskId'], 'attemptId': config['attemptId'], 'state': status['state'],
                'resultDigest': status['resultDigest'], 'receiptDelivered': True, 'quota': 'disabled'}
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN); lock.close()


def run_job(config_path):
    # Backup/restore takes an exclusive lease over the same state root. The
    # worker therefore cannot be half-backed-up between claim and receipt.
    config = private_json(config_path)
    require(isinstance(config.get('stateDir'), str), 'INVALID_STATE_DIR', 400)
    from .recovery import state_lease
    with state_lease(Path(config['stateDir']).resolve(strict=True)):
        return _run_job(config_path)


def main():
    parser = argparse.ArgumentParser(description='Trusted offline DSH runner; no worker control-plane token')
    parser.add_argument('--config', required=True)
    args = parser.parse_args()
    try:
        print(canonical(run_job(args.config)))
    except Exception as exc:
        # Detailed local receipts remain private. CLI never prints secrets or prompts.
        print(canonical({'state': 'blocked', 'error': exc.code if isinstance(exc, Rejected) else type(exc).__name__}))
        raise SystemExit(2)


if __name__ == '__main__':
    main()
