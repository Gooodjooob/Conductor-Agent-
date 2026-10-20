import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from orchestrator.gateway import Gateway, Rejected, manifest
from orchestrator.server import load_config, make_server


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.state = self.root / 'state'; self.state.mkdir(mode=0o700)
        self.work = self.root / 'workspace'; self.work.mkdir()
        self.g = Gateway(str(self.state))
        self.actors = {}
        for role in ('admin', 'worker', 'runner', 'qa', 'reviewer', 'controller'):
            evidence = self.root / role; evidence.mkdir()
            (evidence / 'report.txt').write_text(role + ' independent evidence')
            self.actors[role] = {'id': role + '-1', 'role': role, 'tasks': ['t1', 't2'], 'evidenceRoot': str(evidence)}
        (self.work / 'output.txt').write_text('verified output')
        self.spec = {'taskId': 't1', 'revision': 1, 'issueId': 'issue-1', 'workerId': 'worker-1', 'runnerId': 'runner-1',
                     'qaId': 'qa-1', 'reviewerId': 'reviewer-1', 'controllerId': 'controller-1',
                     'workspace': str(self.work), 'instructionDigest': 'a' * 64, 'promptDigest': hashlib.sha256(b'fixture').hexdigest(), 'artifactPaths': ['output.txt'], 'children': [],
                     'upstreamPolicy': {'reviewPolicy': 'human_only', 'executionPolicy': None, 'parentId': None}}
        self.g.create(self.actors['admin'], self.spec)

    def tearDown(self):
        self.tmp.cleanup()

    def artifact(self, root, name):
        data = (Path(root) / name).read_bytes()
        return {'path': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)}

    def receipt_body(self, task='t1', attempt='a1', state='awaiting_validation'):
        return {'taskId': task, 'revision': 1, 'attemptId': attempt, 'instructionDigest': 'a' * 64,
                'state': state, 'sessionId': 's-' + attempt, 'messageId': 'm-' + attempt,
                'artifacts': [self.artifact(self.work, 'output.txt')] if state == 'awaiting_validation' else []}

    def receive(self, task='t1', attempt='a1'):
        return self.g.receipt(self.actors['runner'], self.receipt_body(task, attempt))

    def approve(self, role, task='t1', decision='pass'):
        current = self.g.get(self.actors[role], task)
        body = {'taskId': task, 'revision': 1, 'attemptId': current['attemptId'], 'resultDigest': current['resultDigest'],
                'decision': decision, 'evidence': [self.artifact(self.actors[role]['evidenceRoot'], 'report.txt')]}
        return self.g.verdict(self.actors[role], body)

    def ready(self, task='t1', attempt='a1'):
        self.receive(task, attempt)
        for role in ('qa', 'reviewer', 'controller'):
            self.approve(role, task)

    def assert_rejected(self, code, operation):
        with self.assertRaises(Rejected) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)

    def test_contract_is_immutable_and_issue_unique(self):
        self.assertEqual(self.g.create(self.actors['admin'], self.spec)['taskId'], 't1')
        changed = dict(self.spec, revision=2)
        self.assert_rejected('IMMUTABLE_CONTRACT', lambda: self.g.create(self.actors['admin'], changed))
        self.assert_rejected('ISSUE_ALREADY_BOUND', lambda: self.g.create(self.actors['admin'], dict(self.spec, taskId='t2')))

    def test_worker_has_no_receipt_verdict_close_or_policy_right(self):
        self.assert_rejected('FORBIDDEN', lambda: self.g.receipt(self.actors['worker'], self.receipt_body()))
        self.assert_rejected('FORBIDDEN', lambda: self.g.finalize(self.actors['worker'], 't1'))
        self.assert_rejected('FORBIDDEN', lambda: self.g.create(self.actors['worker'], self.spec))
        result = self.g.completion_request(self.actors['worker'], 't1')
        self.assertTrue(result['completionRequested']); self.assertEqual(result['state'], 'todo')

    def test_other_identity_or_task_scope_denied(self):
        impostor = dict(self.actors['runner'], id='other')
        self.assert_rejected('IDENTITY_MISMATCH', lambda: self.g.receipt(impostor, self.receipt_body()))
        self.assert_rejected('OUT_OF_SCOPE', lambda: self.g.get(dict(self.actors['worker'], tasks=[]), 't1'))

    def test_stale_attempt_revision_and_terminal_override_rejected(self):
        self.receive()
        self.assert_rejected('EXPLICIT_RETRY_REQUIRED', lambda: self.g.receipt(self.actors['runner'], self.receipt_body(attempt='a2')))
        changed = dict(self.receipt_body(), revision=2)
        self.assert_rejected('CONTRACT_MISMATCH', lambda: self.g.receipt(self.actors['runner'], changed))
        self.assert_rejected('TERMINAL_RECEIPT_IMMUTABLE', lambda: self.g.receipt(self.actors['runner'], self.receipt_body(state='running')))

    def test_unknown_cannot_retry_or_finalize(self):
        self.g.receipt(self.actors['runner'], self.receipt_body(state='unknown'))
        self.assert_rejected('RETRY_UNSAFE', lambda: self.g.retry(self.actors['admin'], 't1'))
        self.assert_rejected('RESULT_NOT_READY', lambda: self.g.finalize(self.actors['controller'], 't1'))

    def test_explicit_failed_retry_revokes_old_attempt(self):
        self.g.receipt(self.actors['runner'], self.receipt_body(state='failed'))
        self.g.retry(self.actors['admin'], 't1')
        self.assert_rejected('ATTEMPT_REUSED', lambda: self.receive())
        self.receive(attempt='a2')
        self.assertEqual(self.g.get(self.actors['worker'], 't1')['attemptId'], 'a2')

    def test_all_three_independent_verdicts_required(self):
        self.receive()
        for role in ('qa', 'reviewer', 'controller'):
            self.assert_rejected('MISSING_' + role.upper(), lambda: self.g.finalize(self.actors['controller'], 't1'))
            self.approve(role)
        self.assertEqual(self.g.finalize(self.actors['controller'], 't1')['state'], 'accepted_pending_sync')

    def test_fail_verdict_cannot_be_overwritten(self):
        self.receive(); self.approve('qa', decision='fail')
        self.assert_rejected('VERDICT_IMMUTABLE', lambda: self.approve('qa'))
        self.assert_rejected('MISSING_QA', lambda: self.g.finalize(self.actors['controller'], 't1'))

    def test_artifact_change_and_evidence_change_block_close(self):
        self.ready()
        (self.work / 'output.txt').write_text('mutated')
        self.assert_rejected('ARTIFACT_CHANGED', lambda: self.g.finalize(self.actors['controller'], 't1'))
        (self.work / 'output.txt').write_text('verified output')
        (Path(self.actors['qa']['evidenceRoot']) / 'report.txt').write_text('tampered')
        self.assert_rejected('ARTIFACT_CHANGED', lambda: self.g.finalize(self.actors['controller'], 't1'))

    def test_symlink_traversal_hardlink_rejected(self):
        original = self.artifact(self.work, 'output.txt')
        (self.work / 'link').symlink_to(self.work / 'output.txt')
        with self.assertRaises(Rejected): manifest(str(self.work), [dict(original, path='link')])
        with self.assertRaises(Rejected): manifest(str(self.work), [dict(original, path='../workspace/output.txt')])
        os.link(self.work / 'output.txt', self.work / 'hard')
        self.assert_rejected('UNSAFE_ARTIFACT', lambda: manifest(str(self.work), [original]))

    def test_snapshot_content_and_restart(self):
        received = self.receive()
        artifact = self.artifact(self.work, 'output.txt')
        self.assertEqual((self.state / 'artifacts' / artifact['sha256']).read_text(), 'verified output')
        other = Gateway(str(self.state))
        self.assertEqual(other.get(self.actors['worker'], 't1')['resultDigest'], received['resultDigest'])

    def test_parent_waits_for_child_sync_and_revalidates_child(self):
        self.g.create(self.actors['admin'], dict(self.spec, taskId='t2', issueId='issue-2', children=['t1']))
        self.ready(); self.ready('t2', 'a2')
        self.assert_rejected('CHILD_NOT_DONE', lambda: self.g.finalize(self.actors['controller'], 't2'))
        self.g.finalize(self.actors['controller'], 't1')
        self.assert_rejected('CHILD_NOT_DONE', lambda: self.g.finalize(self.actors['controller'], 't2'))
        class Upstream:
            def reconcile_and_close(self, expected): return 'synced'
        self.g.flush(self.actors['controller'], 't1', Upstream())
        self.g.finalize(self.actors['controller'], 't2')

    def test_outbox_unknown_survives_restart_then_reconcile(self):
        self.ready(); self.g.finalize(self.actors['controller'], 't1')
        class Upstream:
            calls = 0
            def reconcile_and_close(self, expected):
                self.calls += 1
                if self.calls == 1: raise TimeoutError('response lost after write')
                return 'synced'
        client = Upstream()
        self.assertEqual(self.g.flush(self.actors['controller'], 't1', client)['syncState'], 'sync_unknown')
        other = Gateway(str(self.state))
        self.assertEqual(other.get(self.actors['controller'], 't1')['syncState'], 'sync_unknown')
        self.assertEqual(other.flush(self.actors['controller'], 't1', client)['syncState'], 'synced')
        self.assertEqual(other.get(self.actors['worker'], 't1')['state'], 'done')

    def test_concurrent_finalize_single_outbox(self):
        self.ready()
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda _: self.g.finalize(self.actors['controller'], 't1'), range(16)))
        with self.g.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM outbox').fetchone()[0], 1)

    def test_concurrent_claim_grants_exactly_one_dispatch_permit(self):
        body = {'taskId': 't1', 'revision': 1, 'attemptId': 'claim-a1', 'instructionDigest': 'a' * 64}
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.g.claim(self.actors['runner'], body), range(24)))
        self.assertEqual(sum(r['canSubmit'] for r in results), 1)
        self.assertEqual(self.g.get(self.actors['worker'], 't1')['state'], 'unknown')
        self.assert_rejected('EXPLICIT_RETRY_REQUIRED', lambda: self.g.claim(self.actors['runner'], dict(body, attemptId='claim-a2')))
        restarted = Gateway(str(self.state))
        self.assertFalse(restarted.claim(self.actors['runner'], body)['canSubmit'])
        self.assert_rejected('RETRY_UNSAFE', lambda: self.g.retry(self.actors['admin'], 't1'))

    def test_finalized_exact_receipt_and_verdict_redelivery_are_idempotent(self):
        self.ready(); self.g.finalize(self.actors['controller'], 't1')
        self.assertEqual(self.g.receipt(self.actors['runner'], self.receipt_body())['state'], 'accepted_pending_sync')
        self.assertEqual(self.approve('qa')['state'], 'accepted_pending_sync')
        changed = dict(self.receipt_body(), state='failed', artifacts=[])
        self.assert_rejected('TASK_FROZEN', lambda: self.g.receipt(self.actors['runner'], changed))


class HTTPTests(GatewayTests):
    # Reuse setup helpers without duplicating inherited tests in discovery.
    def test_http_auth_roles_route_and_schema(self):
        config = {'stateDir': str(self.state), 'paperclip': None, 'principals': []}
        tokens = {}
        for role, actor in self.actors.items():
            tokens[role] = role + '-secret-' + 'x' * 32
            config['principals'].append(dict(actor, tokenSha256=hashlib.sha256(tokens[role].encode()).hexdigest()))
        server = make_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def request(method, path, payload=None, role='worker', token=True):
            headers = {'Content-Type': 'application/json'}
            if token: headers['Authorization'] = 'Bearer ' + tokens[role]
            body = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode() if payload is not None else None
            q = urllib.request.Request('http://127.0.0.1:' + str(server.server_port) + path, data=body, headers=headers, method=method)
            try:
                with urllib.request.urlopen(q) as r: return r.status, json.load(r)
            except urllib.error.HTTPError as e: return e.code, json.load(e)
        try:
            self.assertEqual(request('GET', '/v1/tasks/t1', token=False)[0], 401)
            self.assertEqual(request('GET', '/v1/tasks/t1')[0], 200)
            self.assertEqual(request('PATCH', '/api/issues/issue-1', {'status': 'done'})[0], 404)
            self.assertEqual(request('POST', '/v1/tasks/t1/finalize', {})[0], 403)
            self.assertEqual(request('POST', '/v1/receipts', self.receipt_body())[0], 403)
            self.assertEqual(request('POST', '/v1/tasks/t1/completion-request', {'role': 'controller'})[0], 400)
            self.assertEqual(request('POST', '/v1/tasks/t1/completion-request', '{"role":1,"role":2}')[0], 400)
            self.assertEqual(request('POST', '/v1/tasks/t1/completion-request', {})[0], 200)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def test_private_config_and_duplicate_identity(self):
        path = self.root / 'config.json'
        principal = dict(self.actors['worker'], tokenSha256='a' * 64)
        path.write_text(json.dumps({'stateDir': str(self.state), 'paperclip': None, 'principals': [principal]}))
        os.chmod(path, 0o644)
        self.assert_rejected('CONFIG_MUST_BE_PRIVATE', lambda: load_config(str(path)))
        os.chmod(path, 0o600); self.assertEqual(len(load_config(str(path))['principals']), 1)
        second = dict(principal, role='controller', tokenSha256='b' * 64)
        path.write_text(json.dumps({'stateDir': str(self.state), 'paperclip': None, 'principals': [principal, second]}))
        self.assert_rejected('DUPLICATE_IDENTITY', lambda: load_config(str(path)))


# Explicit HTTP suite inherits helpers only, not its parent's test cases.
for _name in tuple(GatewayTests.__dict__):
    if _name.startswith('test_'):
        setattr(HTTPTests, _name, None)

if __name__ == '__main__':
    unittest.main()
