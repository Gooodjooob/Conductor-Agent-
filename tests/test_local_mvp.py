import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from orchestrator.local_mvp import LocalMvp


class LocalMvpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = LocalMvp(Path(self.tmp.name) / "state", 0, 0, demo_plan=True).start()

    def tearDown(self):
        if self.runtime is not None:
            self.runtime.close()
        self.tmp.cleanup()

    def request(self, path, token=None, method="GET"):
        request = urllib.request.Request(
            self.runtime.addresses["dashboard"] + path,
            method=method,
        )
        if token:
            request.add_header("Authorization", "Bearer " + token)
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def test_dashboard_and_snapshot_are_live_and_read_only(self):
        status, html = self.request("/")
        self.assertEqual(status, 200)
        self.assertIn("任务有去向", html.decode())
        self.assertEqual(self.request("/api/snapshot")[0], 401)
        status, raw = self.request("/api/snapshot", self.runtime.dashboard_token)
        self.assertEqual(status, 200)
        snapshot = json.loads(raw)
        self.assertEqual(snapshot["evidenceType"], "demo/fixture")
        self.assertEqual(snapshot["quota"], "disabled")
        self.assertGreaterEqual(len(snapshot["cards"]), 4)
        self.assertEqual(self.request("/api/snapshot", self.runtime.dashboard_token, "POST")[0], 405)

    def test_scheduler_snapshot_contains_expected_fixture_states(self):
        deadline = time.time() + 2
        while time.time() < deadline:
            states = {card["taskId"]: card["state"] for card in self.runtime.snapshot()["cards"]}
            if states.get("demo-01-codex") == "done" and states.get("demo-03-qclaw") == "failed":
                break
            time.sleep(0.05)
        self.assertEqual(states["demo-01-codex"], "done")
        self.assertEqual(states["demo-03-qclaw"], "failed")
        self.assertIn(states["demo-02-workbuddy"], {"running", "awaiting_validation"})
        integration = next(card for card in self.runtime.snapshot()["cards"] if card["taskId"] == "demo-04-integration")
        self.assertEqual(integration["state"], "queued")
        self.assertTrue(integration["blockers"])

    def test_close_stops_service_threads(self):
        gateway_thread = self.runtime.gateway_thread
        dashboard_thread = self.runtime.dashboard_thread
        scheduler_thread = self.runtime.scheduler_thread
        self.runtime.close()
        self.assertFalse(gateway_thread.is_alive())
        self.assertFalse(dashboard_thread.is_alive())
        self.assertFalse(scheduler_thread.is_alive())
        self.runtime = None


if __name__ == "__main__":
    unittest.main()
