import hashlib
import tempfile
import unittest
from pathlib import Path

from orchestrator.openclaw import OpenClawAdapter, OpenClawGatewayTransport, OpenClawError, read_only_policy


class OpenClawTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='openclaw-adapter-')
        self.root = Path(self.tmp.name)
        self.workspace = self.root / 'workspace'; self.workspace.mkdir()
        self.store = self.root / 'store'; self.store.mkdir(mode=0o700)
        self.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def transport(self, handler):
        return OpenClawGatewayTransport(call_impl=handler)

    def make(self, handler):
        return OpenClawAdapter(self.store, self.workspace, self.transport(handler),
                               'agent-dev', 'agent:agent-dev:dispatch:test', model='deepseek-v4-pro')

    def make_read_only(self, handler):
        return OpenClawAdapter(self.store, self.workspace, self.transport(handler),
                               'agent-dev', 'agent:agent-dev:dispatch:test',
                               model='deepseek-v4-pro', require_read_only_policy=True)

    def read_only_config(self, **overrides):
        agent = {
            'id': 'agent-dev',
            'workspace': str(self.workspace),
            'sandbox': {'mode': 'container', 'workspaceAccess': 'ro'},
            'tools': {'deny': ['exec', 'process', 'network']},
        }
        agent.update(overrides)
        return {'config': {'agents': {'defaults': {}, 'list': [agent]}}}

    def test_read_only_policy_requires_explicit_workspace_and_tool_denials(self):
        policy = read_only_policy(self.read_only_config(), 'agent-dev', self.workspace)
        self.assertTrue(policy['safe'])
        unsafe = read_only_policy(self.read_only_config(tools={'deny': ['exec', 'process']}), 'agent-dev', self.workspace)
        self.assertFalse(unsafe['safe'])
        self.assertEqual(unsafe['reason'], 'dangerous_tools_not_disabled')
        sandbox_network = self.read_only_config(tools={'deny': ['exec', 'process']})
        sandbox_network['config']['agents']['list'][0]['sandbox']['network'] = False
        self.assertTrue(read_only_policy(sandbox_network, 'agent-dev', self.workspace)['safe'])
        native_shape = self.read_only_config(tools={
            'exec': {'mode': 'deny'}, 'process': {'mode': 'deny'},
        })
        native_shape['config']['agents']['list'][0]['sandbox']['docker'] = {'network': 'none'}
        self.assertTrue(read_only_policy(native_shape, 'agent-dev', self.workspace)['safe'])

    def test_read_only_preflight_runs_before_agent(self):
        def call(method, params, timeout):
            self.calls.append(method)
            if method == 'config.get': return self.read_only_config()
            if method == 'agent': return {'runId': 'attempt-1', 'status': 'accepted'}
            if method == 'agent.wait': return {'status': 'ok'}
            if method == 'chat.history': return {'messages': [{'role': 'assistant', 'text': 'OPENCLAW_OK'}]}
            raise AssertionError(method)
        adapter = self.make_read_only(call); adapter.start()
        result = adapter.submit('task-1', 1, 'attempt-1', 'ping', 'e' * 64)
        self.assertEqual(result['state'], 'awaiting_validation')
        self.assertEqual(self.calls, ['config.get', 'agent', 'agent.wait', 'chat.history'])

    def test_read_only_preflight_blocks_write_capable_agent_before_agent(self):
        def call(method, params, timeout):
            self.calls.append(method)
            if method == 'config.get': return {'config': {'agents': {'list': [{'id': 'agent-dev'}]}}}
            raise AssertionError(method)
        adapter = self.make_read_only(call)
        with self.assertRaisesRegex(OpenClawError, 'OPENCLAW_READ_ONLY_POLICY_REQUIRED'):
            adapter.start()
        self.assertEqual(self.calls, ['config.get'])

    def test_accept_wait_history_reaches_validation(self):
        def call(method, params, timeout):
            self.calls.append((method, params))
            if method == 'agent': return {'runId': 'attempt-1', 'status': 'accepted'}
            if method == 'agent.wait': return {'status': 'ok'}
            if method == 'chat.history': return {'messages': [{'role': 'assistant', 'content': [{'type': 'text', 'text': 'OPENCLAW_OK'}]}]}
            raise AssertionError(method)
        adapter = self.make(call); adapter.start()
        result = adapter.submit('task-1', 1, 'attempt-1', 'ping', 'a' * 64)
        self.assertEqual(result['state'], 'awaiting_validation')
        self.assertTrue(result['accepted'])
        self.assertEqual([item[0] for item in self.calls], ['agent', 'agent.wait', 'chat.history'])

    def test_transport_loss_recovers_exact_run(self):
        def call(method, params, timeout):
            if method == 'agent': raise OpenClawError('transport down')
            if method == 'tasks.list': return {'tasks': [{'runId': 'attempt-1', 'taskId': 'remote-1', 'status': 'running'}]}
            if method == 'agent.wait': return {'status': 'ok'}
            if method == 'chat.history': return {'messages': [{'role': 'assistant', 'text': 'recovered'}]}
            raise AssertionError(method)
        adapter = self.make(call); adapter.start()
        self.assertEqual(adapter.submit('task-1', 1, 'attempt-1', 'ping', 'b' * 64)['state'], 'awaiting_validation')

    def test_timeout_requires_exact_cancel_confirmation(self):
        def call(method, params, timeout):
            if method == 'agent': return {'runId': 'attempt-1', 'status': 'accepted'}
            if method == 'agent.wait': return {'status': 'timeout'}
            if method == 'tasks.list': return {'tasks': [{'runId': 'attempt-1', 'taskId': 'remote-1', 'status': 'running'}]}
            if method == 'tasks.cancel': return {'found': True, 'cancelled': False}
            raise AssertionError(method)
        adapter = self.make(call); adapter.start()
        result = adapter.submit('task-1', 1, 'attempt-1', 'ping', 'c' * 64)
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['reason'], 'OPENCLAW_TIMEOUT_CANCEL_UNCONFIRMED')

    def test_history_content_and_artifact_manifest(self):
        (self.workspace / 'output.txt').write_text('artifact')
        adapter = self.make(lambda method, params, timeout: {'runId': 'attempt-1', 'status': 'accepted'} if method == 'agent' else {'status': 'ok'} if method == 'agent.wait' else {'messages': [{'role': 'assistant', 'text': 'ok'}]})
        adapter.start(); adapter.submit('task-1', 1, 'attempt-1', 'ping', 'd' * 64)
        result = adapter.collect('attempt-1', ['output.txt'])
        self.assertEqual(result['artifacts'][0]['sha256'], hashlib.sha256(b'artifact').hexdigest())

    def test_transport_requires_absolute_command_without_fixture(self):
        with self.assertRaisesRegex(ValueError, 'ABSOLUTE_OPENCLAW_COMMAND_REQUIRED'):
            OpenClawGatewayTransport(command=['openclaw'], state_dir='/tmp/state')


if __name__ == '__main__':
    unittest.main()
