import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from orchestrator.gateway import Gateway, Rejected
from orchestrator.runner import run_job, GatewayClient, durable_json
from orchestrator.server import make_server


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='orchestrator-p0-e2e-')
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.work = self.root / 'work'; self.work.mkdir()
        self.token = 'synthetic-runner-key-' + 'x' * 32
        self.actor = {'id': 'runner-1', 'role': 'runner', 'tasks': ['t1']}
        self.server = make_server({'stateDir': str(self.state), 'paperclip': None,
                    'principals': [dict(self.actor, tokenSha256=hashlib.sha256(self.token.encode()).hexdigest())]})
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        fixture = Path(__file__).parent / 'fixtures' / 'isolated_sdk.py'
        self.spec = {'taskId': 't1', 'revision': 1, 'issueId': 'fixture-issue',
                     'workerId': 'worker-1', 'runnerId': 'runner-1', 'qaId': 'qa-1', 'reviewerId': 'reviewer-1', 'controllerId': 'controller-1',
                     'workspace': str(self.work), 'instructionDigest': 'a' * 64,
                     'promptDigest': hashlib.sha256(b'fixture prompt').hexdigest(), 'artifactPaths': ['output.txt'], 'children': [],
                     'upstreamPolicy': {'reviewPolicy': None, 'executionPolicy': None, 'parentId': None}}
        self.g = self.server.gateway
        self.g.create({'id': 'admin-1', 'role': 'admin', 'tasks': ['*']}, self.spec)
        self.config = {'stateDir': str(self.state), 'gatewayUrl': 'http://127.0.0.1:' + str(self.server.server_port),
                       'gatewayToken': self.token, 'taskId': 't1', 'attemptId': 'attempt-1', 'prompt': 'fixture prompt',
                       'command': [sys.executable, str(fixture.resolve())], 'runtimeRoots': [sys.prefix, str(fixture.parent.resolve())], 'timeout': 5}
        self.path = self.state / 'job-config.json'; self.write_config()

    def write_config(self):
        self.path.write_text(json.dumps(self.config)); os.chmod(self.path, 0o600)

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(); self.tmp.cleanup()

    def test_real_isolated_sdk_gateway_and_duplicate_recovery(self):
        result = run_job(str(self.path))
        self.assertEqual(result['state'], 'awaiting_validation')
        self.assertEqual((self.work / 'submissions.txt').read_text(), 'accepted\n')
        self.assertEqual(run_job(str(self.path))['resultDigest'], result['resultDigest'])
        self.assertEqual((self.work / 'submissions.txt').read_text(), 'accepted\n')
        self.assertNotIn(self.token, (self.work / 'output.txt').read_text())
        self.assertEqual(self.g.get(self.actor, 't1')['state'], 'awaiting_validation')

    def test_prompt_change_rejected_before_execution(self):
        self.config['prompt'] = 'changed scope'; self.write_config()
        with self.assertRaisesRegex(Rejected, 'PROMPT_CONTRACT_MISMATCH'): run_job(str(self.path))
        self.assertFalse((self.work / 'submissions.txt').exists())

    def test_receipt_response_lost_retries_delivery_without_sdk(self):
        original = GatewayClient.request
        def lose(client, path, body=None):
            result = original(client, path, body)
            if path == '/v1/receipts':
                raise Rejected('GATEWAY_UNREACHABLE')
            return result
        with patch.object(GatewayClient, 'request', lose):
            with self.assertRaisesRegex(Rejected, 'GATEWAY_UNREACHABLE'):
                run_job(str(self.path))
        self.assertEqual((self.work / 'submissions.txt').read_text(), 'accepted\n')
        self.assertEqual(run_job(str(self.path))['state'], 'awaiting_validation')
        self.assertEqual((self.work / 'submissions.txt').read_text(), 'accepted\n')

    def test_claim_response_lost_does_not_guess_or_dispatch(self):
        original = GatewayClient.request
        def lose(client, path, body=None):
            result = original(client, path, body)
            if path == '/v1/claims': raise Rejected('GATEWAY_UNREACHABLE')
            return result
        with patch.object(GatewayClient, 'request', lose):
            with self.assertRaisesRegex(Rejected, 'GATEWAY_UNREACHABLE'): run_job(str(self.path))
        with self.assertRaisesRegex(Rejected, 'REMOTE_ATTEMPT_ALREADY_CLAIMED_NO_REPLAY'): run_job(str(self.path))
        self.assertFalse((self.work / 'submissions.txt').exists())

    def test_restart_after_collect_before_pending_preserves_launch_binding(self):
        def crash(path, value):
            if path.name == 'pending.json': raise OSError('synthetic crash before receipt spool')
            return durable_json(path, value)
        with patch('orchestrator.runner.durable_json', side_effect=crash):
            with self.assertRaises(OSError): run_job(str(self.path))
        self.assertEqual((self.work / 'submissions.txt').read_text(), 'accepted\n')
        self.assertEqual(run_job(str(self.path))['state'], 'awaiting_validation')
        self.assertEqual((self.work / 'submissions.txt').read_text(), 'accepted\n')

    def test_worker_identity_cannot_launch_trusted_runner(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        config = {'stateDir': str(self.state), 'paperclip': None, 'principals': [{'id': 'worker-1', 'role': 'worker', 'tasks': ['t1'], 'tokenSha256': hashlib.sha256(self.token.encode()).hexdigest()}]}
        self.server = make_server(config); self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.config['gatewayUrl'] = 'http://127.0.0.1:' + str(self.server.server_port); self.write_config()
        with self.assertRaisesRegex(Rejected, 'RUNNER_IDENTITY_REQUIRED'): run_job(str(self.path))
        self.assertFalse((self.work / 'submissions.txt').exists())

    def _write_cli_fixture(self, name, body):
        # Keep the fixture inside the granted workspace so the isolation
        # profile can read it without widening runtimeRoots over state.
        fixture = self.work / name
        fixture.write_text(body)
        os.chmod(fixture, 0o700)
        return fixture

    def test_codex_provider_route_uses_jsonl_adapter(self):
        fixture = self._write_cli_fixture('codex-runner-fixture.py', '''import json
from pathlib import Path
for event in (
    {"type": "thread.started", "thread_id": "fixture-thread"},
    {"type": "item.completed", "item": {"type": "agent_message", "text": "codex fixture"}},
    {"type": "turn.completed"},
):
    print(json.dumps(event), flush=True)
Path("output.txt").write_text("codex artifact\\n")
''')
        self.config.update(provider='codex', sandbox='read-only', command=[sys.executable, str(fixture)])
        self.write_config()
        result = run_job(str(self.path))
        self.assertEqual(result['state'], 'awaiting_validation')
        self.assertEqual((self.work / 'output.txt').read_text(), 'codex artifact\n')

    def test_workbuddy_provider_route_uses_codebuddy_adapter(self):
        fixture = self._write_cli_fixture('workbuddy-runner-fixture.py', '''import json
from pathlib import Path
print(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                  "result": "workbuddy fixture"}), flush=True)
Path("output.txt").write_text("workbuddy artifact\\n")
''')
        self.config.update(provider='workbuddy', model='deepseek-v4-pro', sandbox='read-only', command=[sys.executable, str(fixture)])
        self.write_config()
        result = run_job(str(self.path))
        self.assertEqual(result['state'], 'awaiting_validation')
        self.assertEqual((self.work / 'output.txt').read_text(), 'workbuddy artifact\n')

    def test_clawhive_provider_is_fail_closed(self):
        self.config['provider'] = 'clawhive'
        self.write_config()
        with self.assertRaisesRegex(Rejected, 'UNSUPPORTED_PROVIDER'):
            run_job(str(self.path))
        self.assertFalse((self.work / 'submissions.txt').exists())

    def test_openclaw_provider_uses_trusted_control_plane_bridge(self):
        bridge = self.work / 'openclaw-bridge.py'
        bridge.write_text('''import json, sys
from pathlib import Path
params = json.loads(sys.argv[sys.argv.index("--params") + 1])
method = sys.argv[sys.argv.index("call") + 1]
if method == "agent":
    result = {"runId": params["idempotencyKey"], "status": "accepted"}
elif method == "agent.wait":
    Path("output.txt").write_text("openclaw artifact\\n")
    result = {"status": "ok"}
elif method == "chat.history":
    result = {"messages": [{"role": "assistant", "text": "openclaw fixture"}]}
else:
    result = {"tasks": []}
print(json.dumps(result), flush=True)
''')
        os.chmod(bridge, 0o700)
        openclaw_state = self.root / 'openclaw-state'; openclaw_state.mkdir(mode=0o700)
        self.config = {k: v for k, v in self.config.items() if k not in ('command', 'runtimeRoots')}
        self.config.update(provider='openclaw', model='deepseek-v4-pro',
                           openclawCommand=[sys.executable, str(bridge)],
                           openclawStateDir=str(openclaw_state),
                           openclawAgentId='agent-dev',
                           openclawSessionKey='agent:agent-dev:dispatch:t1')
        self.write_config()
        result = run_job(str(self.path))
        self.assertEqual(result['state'], 'awaiting_validation')
        self.assertEqual((self.work / 'output.txt').read_text(), 'openclaw artifact\n')

    def test_openclaw_provider_rejects_shared_state_dir(self):
        self.config = {k: v for k, v in self.config.items() if k not in ('command', 'runtimeRoots')}
        self.config.update(provider='openclaw', openclawCommand=[sys.executable],
                           openclawStateDir=str(self.state), openclawAgentId='agent-dev',
                           openclawSessionKey='agent:agent-dev:dispatch:t1')
        self.write_config()
        with self.assertRaisesRegex(Rejected, 'OPENCLAW_STATE_DIR_MUST_BE_DISJOINT'):
            run_job(str(self.path))
