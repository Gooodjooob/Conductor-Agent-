"""Explicit no-credential canary; creates only isolated local test state."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.gateway import Rejected
from orchestrator.paperclip import Paperclip
from orchestrator.runner import run_job
from orchestrator.server import make_server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--node', required=True)
    parser.add_argument('--dsh', required=True)
    parser.add_argument('--modules', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix='orchestrator-p0-live-', dir='/private/tmp')).resolve()
    state = root / 'state'; state.mkdir(mode=0o700)
    work = root / 'work'; work.mkdir()
    token = os.urandom(32).hex()
    actor = {'id': 'canary-runner', 'role': 'runner', 'tasks': ['canary-task']}
    server = make_server({'stateDir': str(state), 'principals': [dict(actor, tokenSha256=hashlib.sha256(token.encode()).hexdigest())], 'paperclip': None})
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    prompt = 'Protocol canary only. Do not use tools or change files. Reply OK.'
    spec = {'taskId': 'canary-task', 'revision': 1, 'issueId': 'canary-no-upstream-write',
            'workerId': 'canary-worker', 'runnerId': 'canary-runner', 'qaId': 'canary-qa', 'reviewerId': 'canary-reviewer', 'controllerId': 'canary-controller',
            'workspace': str(work), 'instructionDigest': hashlib.sha256(b'offline-canary-no-key').hexdigest(),
            'promptDigest': hashlib.sha256(prompt.encode()).hexdigest(), 'artifactPaths': ['output.txt'], 'children': [],
            'upstreamPolicy': {'reviewPolicy': None, 'executionPolicy': None, 'parentId': None}}
    report = {'mode': 'real-sdk-offline-no-credential', 'root': str(root)}
    try:
        server.gateway.create({'id': 'admin', 'role': 'admin', 'tasks': ['*']}, spec)
        config = {'stateDir': str(state), 'gatewayUrl': 'http://127.0.0.1:' + str(server.server_port), 'gatewayToken': token,
                  'taskId': 'canary-task', 'attemptId': 'canary-attempt', 'prompt': prompt,
                  'command': [str(Path(args.node).resolve()), str(Path(args.dsh).resolve()), '--profile', 'sdk'],
                  'runtimeRoots': [str(Path(args.node).resolve().parents[1]), str(Path(args.modules).resolve())], 'timeout': 15}
        path = state / 'runner.json'; path.write_text(json.dumps(config)); os.chmod(path, 0o600)
        report['first'] = run_job(str(path))
        report['second'] = run_job(str(path))
        assert report['first']['state'] == 'failed', report['first']
        assert report['first']['resultDigest'] == report['second']['resultDigest']
        report['gateway'] = {k: v for k, v in server.gateway.get(actor, 'canary-task').items() if k != 'contract'}
        # Reject the existing local_trusted service before supplying a real token.
        try:
            Paperclip('http://127.0.0.1:3100', 'synthetic-probe-not-a-real-key').assert_boundary()
        except Rejected as exc:
            report['existingPaperclip'] = exc.code
        else:
            report['existingPaperclip'] = 'unexpected-safe-mode-needs-separate-configuration'
    except Exception as exc:
        report['error'] = exc.code if isinstance(exc, Rejected) else type(exc).__name__
        raise
    finally:
        server.shutdown(); server.server_close(); thread.join()
        Path(args.output).write_text(json.dumps(report, indent=2))
        print(json.dumps(report))


if __name__ == '__main__':
    main()
