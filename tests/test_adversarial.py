"""Independent QA: negative contracts, HTTP authority, and upstream evidence."""
import copy
import concurrent.futures
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import tempfile
import threading
import time
import unittest

from orchestrator.gateway import Gateway, Rejected
from orchestrator.paperclip import Paperclip
from orchestrator.server import make_server, load_config
from orchestrator.dsh import DSHAdapter
from orchestrator.isolation import prepare_launch


class AdversarialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="orch-independent-qa-")
        self.root = Path(self.temp.name).resolve()
        self.work = self.root / "work"
        self.work.mkdir()
        self.state = self.root / "state"
        self.gateway = Gateway(str(self.state))
        self.actors = {role: {"id": role + "-one", "role": role, "tasks": ["task-one"]}
                       for role in ("admin", "worker", "runner", "qa", "reviewer", "controller")}
        self.spec = {"taskId": "task-one", "revision": 1, "issueId": "issue-one",
                     "workspace": str(self.work), "instructionDigest": "a" * 64,
                     "promptDigest": "b" * 64, "artifactPaths": ["result.py"],
                     "children": [], "upstreamPolicy": {"reviewPolicy": None,
                     "executionPolicy": None, "parentId": None}}
        for role in ("worker", "runner", "qa", "reviewer", "controller"):
            self.spec[role + "Id"] = self.actors[role]["id"]
        self.gateway.create(self.actors["admin"], self.spec)
        self.receipt = {"taskId": "task-one", "revision": 1, "attemptId": "attempt-one",
                        "instructionDigest": "a" * 64, "state": "running",
                        "sessionId": "session-one", "messageId": "message-one", "artifacts": []}

    def tearDown(self):
        self.temp.cleanup()

    def test_revision_boolean_float_string_rejected(self):
        for revision in (True, 1.0, "1"):
            with self.subTest(revision=revision):
                body = dict(self.receipt, revision=revision)
                with self.assertRaises(Rejected):
                    self.gateway.receipt(self.actors["runner"], body)

    def test_worker_cannot_write_runtime_verdict_or_finalize(self):
        with self.assertRaises(Rejected):
            self.gateway.receipt(self.actors["worker"], self.receipt)
        with self.assertRaises(Rejected):
            self.gateway.finalize(self.actors["worker"], "task-one")
        with self.assertRaises(Rejected):
            self.gateway.verdict(self.actors["worker"], {"taskId": "task-one", "revision": 1,
                "attemptId": "attempt-one", "resultDigest": "b" * 64, "decision": "pass", "evidence": []})

    def test_scope_and_bound_identity_are_both_required(self):
        with self.assertRaises(Rejected):
            self.gateway.get(dict(self.actors["worker"], tasks=["another-task"]), "task-one")
        with self.assertRaises(Rejected):
            self.gateway.get(dict(self.actors["worker"], id="another-worker"), "task-one")

    def test_same_attempt_failed_cannot_be_upgraded(self):
        self.gateway.receipt(self.actors["runner"], dict(self.receipt, state="failed"))
        with self.assertRaises(Rejected):
            self.gateway.receipt(self.actors["runner"], self.receipt)
        with self.assertRaises(Rejected):
            self.gateway.receipt(self.actors["runner"], dict(self.receipt, sessionId="other-session"))

    def test_parallel_distinct_attempts_get_one_dispatch_claim(self):
        def claim(index):
            body = {"taskId": "task-one", "revision": 1, "attemptId": "attempt-" + str(index), "instructionDigest": "a" * 64}
            try:
                return self.gateway.claim(self.actors["runner"], body)["canSubmit"]
            except Rejected:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
            results = list(pool.map(claim, range(24)))
        self.assertEqual(sum(results), 1)

    def test_contract_change_and_policy_removal_rejected(self):
        changed = copy.deepcopy(self.spec)
        changed["upstreamPolicy"] = {"reviewPolicy": None, "executionPolicy": {}, "parentId": None}
        with self.assertRaises(Rejected):
            self.gateway.create(self.actors["admin"], changed)
        changed = dict(self.spec, revision=2)
        with self.assertRaises(Rejected):
            self.gateway.create(self.actors["admin"], changed)

    def test_http_role_spoof_patch_and_duplicate_keys_rejected(self):
        token = "synthetic-worker-token-long-enough"
        config = {"stateDir": str(self.state), "paperclip": None,
                  "principals": [dict(self.actors["worker"], tokenSha256=hashlib.sha256(token.encode()).hexdigest())]}
        server = make_server(config)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def request(method, path, body=None, extra=None):
                connection = http.client.HTTPConnection(*server.server_address, timeout=3)
                headers = {"Authorization": "Bearer " + token}
                headers.update(extra or {})
                connection.request(method, path, body=body, headers=headers)
                response = connection.getresponse()
                value = response.status, json.loads(response.read())
                connection.close()
                return value
            self.assertEqual(request("GET", "/v1/tasks/task-one")[0], 200)
            self.assertEqual(request("POST", "/v1/tasks/task-one/finalize", "{}")[0], 403)
            self.assertEqual(request("POST", "/v1/tasks/task-one/completion-request", '{"role":"controller"}')[0], 400)
            self.assertEqual(request("POST", "/v1/tasks/task-one/completion-request", '{"x":1,"x":2}')[1]["error"], "DUPLICATE_JSON_KEY")
            self.assertEqual(request("PATCH", "/api/issues/issue-one", '{"status":"done"}')[0], 404)
            self.assertEqual(request("GET", "/v1/tasks/task-one", extra={"Origin": "http://attacker.invalid"})[0], 403)
            self.assertEqual(request("GET", "/v1/tasks/task-one", extra={"Authorization": "Bearer " + "x" * 32})[0], 401)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_config_rejects_shared_identity_and_unscoped_worker(self):
        principal = dict(self.actors["worker"], tokenSha256="a" * 64)
        path = self.root / "config.json"
        for principals in ([principal, dict(principal, role="controller", tokenSha256="b" * 64)],
                           [dict(principal, tasks=["*"])]):
            path.write_text(json.dumps({"stateDir": str(self.state), "paperclip": None, "principals": principals}))
            path.chmod(0o600)
            with self.assertRaises(Rejected):
                load_config(str(path))

    def test_upstream_done_without_exact_acceptance_marker_rejected(self):
        client = Paperclip("http://127.0.0.1:9999", "synthetic-controller-token-long")
        client.assert_boundary = lambda: None
        client.request = lambda *args, **kwargs: (200, {"id": "issue-one", "status": "done",
            "description": "worker said completed", "reviewPolicy": None, "executionPolicy": None, "parentId": None})
        with self.assertRaisesRegex(Rejected, "UNVERIFIED_UPSTREAM_DONE"):
            client.reconcile_and_close({"issueId": "issue-one", "resultDigest": "a" * 64, "policy": self.spec["upstreamPolicy"]})

    def test_upstream_policy_drift_rejected_before_write(self):
        client = Paperclip("http://127.0.0.1:9999", "synthetic-controller-token-long")
        client.assert_boundary = lambda: None
        calls = []
        def request(method, *args, **kwargs):
            calls.append(method)
            return 200, {"id": "issue-one", "status": "todo", "description": "", "reviewPolicy": "changed", "executionPolicy": None, "parentId": None}
        client.request = request
        with self.assertRaisesRegex(Rejected, "UPSTREAM_POLICY_DRIFT"):
            client.reconcile_and_close({"issueId": "issue-one", "resultDigest": "a" * 64, "policy": self.spec["upstreamPolicy"]})
        self.assertEqual(calls, ["GET"])

    @unittest.skipUnless(platform.system() == "Darwin", "real Seatbelt integration")
    def test_real_sandbox_adapter_collect_is_not_gateway_completion(self):
        fixture = Path(__file__).parent.resolve() / "fixtures" / "sdk_fixture.py"
        spec = prepare_launch([str(Path(sys.executable).resolve()), str(fixture), "completed"],
                              str(self.work), [str(Path(sys.prefix).resolve()), str(fixture.parent)], str(self.state))
        adapter = DSHAdapter(str(self.state / "attempt-one"), spec.command, spec.cwd, spec.env, timeout=2)
        try:
            self.assertTrue(adapter.start()["protocolReady"])
            adapter.submit("task-one", 1, "attempt-one", "synthetic QA fixture", "a" * 64)
            deadline = time.monotonic() + 3
            while adapter.get_status("attempt-one")["state"] not in {"unknown", "failed", "awaiting_validation"} and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(adapter.get_status("attempt-one")["state"], "awaiting_validation")
            (self.work / "result.py").write_text("print('synthetic fixture')\n")
            collected = adapter.collect("attempt-one", ["result.py"])
            self.assertFalse(collected["qaPassed"])
            self.assertTrue(collected["runtimeClosed"])
            receipt = {key: collected[key] for key in self.receipt}
            self.gateway.receipt(self.actors["runner"], receipt)
            with self.assertRaises(Rejected):
                self.gateway.finalize(self.actors["controller"], "task-one")
            self.assertEqual(self.gateway.get(self.actors["worker"], "task-one")["state"], "awaiting_validation")
        finally:
            adapter.close()
            shutil.rmtree(spec.home, ignore_errors=True)


@unittest.skipUnless(platform.system() == "Darwin", "real Seatbelt integration")
class RunnerAdversarialTests(unittest.TestCase):
    def test_new_attempt_does_not_execute_before_gateway_rejects(self):
        import test_runner
        from orchestrator.runner import run_job
        harness = test_runner.RunnerTests(methodName="runTest")
        harness.setUp()
        try:
            run_job(str(harness.path))
            harness.config["attemptId"] = "attempt-two"
            harness.write_config()
            with self.assertRaises(Rejected):
                run_job(str(harness.path))
            self.assertEqual((harness.work / "submissions.txt").read_text(), "accepted\n")
        finally:
            harness.tearDown()

    def test_missing_local_ledger_does_not_reexecute_remote_attempt(self):
        import test_runner
        from orchestrator.runner import run_job
        harness = test_runner.RunnerTests(methodName="runTest")
        harness.setUp()
        try:
            run_job(str(harness.path))
            other_state = harness.root / "different-runner-state"
            other_state.mkdir(mode=0o700)
            harness.config["stateDir"] = str(other_state)
            harness.path = other_state / "config.json"
            harness.write_config()
            with self.assertRaises(Rejected):
                run_job(str(harness.path))
            self.assertEqual((harness.work / "submissions.txt").read_text(), "accepted\n")
        finally:
            harness.tearDown()


if __name__ == "__main__":
    unittest.main()
