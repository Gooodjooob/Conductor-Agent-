import copy
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from orchestrator.gateway import Rejected
from orchestrator.paperclip import Paperclip


class PaperclipTests(unittest.TestCase):
    def setUp(self):
        self.state = {'mode': 'authenticated', 'anonymous': False, 'writes': 0, 'loseResponse': False,
                      'issue': {'id': 'issue-1', 'status': 'in_review', 'description': 'original', 'reviewPolicy': 'human_only', 'executionPolicy': None, 'parentId': None}}
        state = self.state
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def send(self, code, body):
                raw = json.dumps(body).encode(); self.send_response(code); self.end_headers(); self.wfile.write(raw)
            def do_GET(self):
                if self.path == '/api/health': return self.send(200, {'deploymentMode': state['mode'], 'authReady': True})
                if not self.headers.get('Authorization') and not state['anonymous']: return self.send(401, {})
                if self.path == '/api/companies': return self.send(200, [])
                return self.send(200, state['issue'])
            def do_PATCH(self):
                state['writes'] += 1
                state['issue'].update(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
                if state['loseResponse']:
                    state['loseResponse'] = False; self.close_connection = True; return
                self.send(200, state['issue'])
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True); self.thread.start()
        self.client = Paperclip('http://127.0.0.1:' + str(self.server.server_port), 'trusted-secret-' + 'x' * 32)
        self.expected = {'issueId': 'issue-1', 'resultDigest': 'a' * 64, 'policy': {'reviewPolicy': 'human_only', 'executionPolicy': None, 'parentId': None}}

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()

    def test_local_trusted_refused(self):
        self.state['mode'] = 'local_trusted'
        with self.assertRaisesRegex(Rejected, 'UNSAFE_UPSTREAM_MODE'): self.client.reconcile_and_close(self.expected)
        self.assertEqual(self.state['writes'], 0)

    def test_false_authenticated_but_anonymous_access_refused(self):
        self.state['anonymous'] = True
        with self.assertRaisesRegex(Rejected, 'ANONYMOUS_UPSTREAM_ACCESS'): self.client.reconcile_and_close(self.expected)

    def test_policy_drift_and_unverified_done_refused(self):
        self.state['issue']['reviewPolicy'] = 'anyone'
        with self.assertRaisesRegex(Rejected, 'UPSTREAM_POLICY_DRIFT'): self.client.reconcile_and_close(self.expected)
        self.state['issue']['reviewPolicy'] = 'human_only'; self.state['issue']['status'] = 'done'
        with self.assertRaisesRegex(Rejected, 'UNVERIFIED_UPSTREAM_DONE'): self.client.reconcile_and_close(self.expected)

    def test_lost_write_response_reconciles_without_second_patch(self):
        self.state['loseResponse'] = True
        with self.assertRaises(Exception): self.client.reconcile_and_close(self.expected)
        self.assertEqual(self.client.reconcile_and_close(self.expected), 'synced')
        self.assertEqual(self.state['writes'], 1)
        self.assertEqual(self.client.reconcile_and_close(self.expected), 'synced')
        self.assertEqual(self.state['writes'], 1)

    def test_close_preserves_description_and_policy(self):
        self.assertEqual(self.client.reconcile_and_close(self.expected), 'synced')
        self.assertTrue(self.state['issue']['description'].startswith('original'))
        self.assertEqual(self.state['issue']['reviewPolicy'], 'human_only')

    def test_unsafe_url_and_header_token_refused(self):
        for url in ('http://example.com', 'https://user:pass@example.com', 'http://127.0.0.1/api', 'http://127.0.0.1?x=1'):
            with self.assertRaises(Rejected): Paperclip(url, 'x' * 32)
        with self.assertRaises(Rejected): Paperclip('http://127.0.0.1', 'x' * 32 + '\n')

    def test_reopened_remote_child_blocks_parent_before_write(self):
        self.expected['children'] = [dict(self.expected)]
        with self.assertRaisesRegex(Rejected, 'UPSTREAM_CHILD_NOT_DONE'):
            self.client.reconcile_and_close(self.expected)
        self.assertEqual(self.state['writes'], 0)
