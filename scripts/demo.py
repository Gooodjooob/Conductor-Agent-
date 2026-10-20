"""Offline end-to-end synthetic SDK + synthetic authenticated Paperclip demo."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from orchestrator.gateway import Rejected
from orchestrator.runner import GatewayClient, run_job
from orchestrator.server import make_server, load_config


def run_demo(output=None):
    package = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix='orchestrator-p0-demo-', dir='/private/tmp')).resolve()
    state = root / 'control'; state.mkdir(mode=0o700)
    work = root / 'workspace'; work.mkdir()
    issue = {'id': 'demo-issue', 'status': 'in_review', 'description': 'SYNTHETIC P0 integration demo',
             'reviewPolicy': 'human_only', 'executionPolicy': None, 'parentId': None}
    upstream_token = os.urandom(32).hex()
    writes = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def reply(self, status, body):
            raw = json.dumps(body).encode(); self.send_response(status); self.end_headers(); self.wfile.write(raw)
        def do_GET(self):
            if self.path == '/api/health': return self.reply(200, {'deploymentMode': 'authenticated', 'authReady': True})
            if self.headers.get('Authorization') != 'Bearer ' + upstream_token: return self.reply(401, {})
            if self.path == '/api/issues/demo-issue': return self.reply(200, issue)
            return self.reply(404, {})
        def do_PATCH(self):
            if self.headers.get('Authorization') != 'Bearer ' + upstream_token: return self.reply(401, {})
            if self.path != '/api/issues/demo-issue': return self.reply(404, {})
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            writes.append(body); issue.update(body); self.reply(200, issue)
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True); upstream_thread.start()
    config = {'stateDir': str(state), 'paperclip': {'url': 'http://127.0.0.1:' + str(upstream.server_port), 'token': upstream_token}, 'principals': []}
    tokens = {}
    for role in ('admin', 'worker', 'runner', 'qa', 'reviewer', 'controller'):
        tokens[role] = os.urandom(32).hex()
        evidence = state / role; evidence.mkdir(mode=0o700)
        config['principals'].append({'id': role + '-1', 'role': role, 'tasks': ['demo-task'], 'evidenceRoot': str(evidence), 'tokenSha256': hashlib.sha256(tokens[role].encode()).hexdigest()})
    path = state / 'gateway.json'; path.write_text(json.dumps(config)); os.chmod(path, 0o600)
    gateway = make_server(load_config(str(path)))
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True); gateway_thread.start()
    url = 'http://127.0.0.1:' + str(gateway.server_port)
    clients = {role: GatewayClient(url, token) for role, token in tokens.items()}
    report = {'evidenceType': 'synthetic-sdk-and-upstream-real-sandbox-and-http', 'root': str(root)}
    try:
        prompt = 'Generate the synthetic fixture artifact.'
        spec = {'taskId': 'demo-task', 'revision': 1, 'issueId': 'demo-issue', 'workspace': str(work),
                'instructionDigest': hashlib.sha256(b'P0 synthetic offline rules').hexdigest(), 'promptDigest': hashlib.sha256(prompt.encode()).hexdigest(),
                'artifactPaths': ['output.txt'], 'children': [], 'upstreamPolicy': {k: issue[k] for k in ('reviewPolicy', 'executionPolicy', 'parentId')}}
        spec.update({role + 'Id': role + '-1' for role in ('worker', 'runner', 'qa', 'reviewer', 'controller')})
        clients['admin'].request('/v1/tasks', spec)
        try: clients['worker'].request('/v1/tasks/demo-task/finalize', {})
        except Rejected as exc: report['workerClose'] = exc.code
        else: raise AssertionError('worker closed task')
        fixture = package / 'tests/fixtures/isolated_sdk.py'
        job = {'stateDir': str(state), 'gatewayUrl': url, 'gatewayToken': tokens['runner'], 'taskId': 'demo-task',
               'attemptId': 'demo-attempt', 'prompt': prompt, 'command': [sys.executable, str(fixture)],
               'runtimeRoots': [sys.prefix, str(fixture.parent)], 'timeout': 5}
        job_path = state / 'job.json'; job_path.write_text(json.dumps(job)); os.chmod(job_path, 0o600)
        report['execution'] = run_job(str(job_path))
        try: clients['controller'].request('/v1/tasks/demo-task/finalize', {})
        except Rejected as exc: report['missingQA'] = exc.code
        else: raise AssertionError('closed without QA')
        current = clients['controller'].request('/v1/tasks/demo-task')
        for role in ('qa', 'reviewer', 'controller'):
            # Explicit fixture attestations, not claims of a human/code review.
            evidence = ('SYNTHETIC ' + role + ' attestation for ' + current['resultDigest']).encode()
            (state / role / 'evidence.txt').write_bytes(evidence)
            clients[role].request('/v1/verdicts', {'taskId': 'demo-task', 'revision': 1, 'attemptId': current['attemptId'],
                    'resultDigest': current['resultDigest'], 'decision': 'pass',
                    'evidence': [{'path': 'evidence.txt', 'sha256': hashlib.sha256(evidence).hexdigest(), 'size': len(evidence)}]})
        report['finalize'] = clients['controller'].request('/v1/tasks/demo-task/finalize', {})
        report['sync'] = clients['controller'].request('/v1/tasks/demo-task/sync', {})
        clients['controller'].request('/v1/tasks/demo-task/sync', {})
        report['readback'] = clients['worker'].request('/v1/tasks/demo-task')
        report['upstreamPatchCount'] = len(writes)
        assert report['readback']['state'] == 'done' and len(writes) == 1
        report['passed'] = True
    finally:
        gateway.shutdown(); gateway.server_close(); gateway_thread.join()
        upstream.shutdown(); upstream.server_close(); upstream_thread.join()
        (root / 'report.json').write_text(json.dumps(report, indent=2))
        if output: Path(output).write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return report


if __name__ == '__main__':
    run_demo(sys.argv[1] if len(sys.argv) > 1 else None)
