"""D4 真实 CLI 入口门禁：使用真实 Node 检查器，Gateway RPC 由离线替身记录。"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from adapters.qclaw_gateway import CliGatewayTransport, GatewayTransportError, QClawGatewayAdapter
from scripts import d4ctl
from storage.sqlite_store import SqliteStore

from test_d4ctl import D4CtlTestCase, ResponsiveTransport, delivered_payload, event_payload


REAL_RUN = subprocess.run
DENY = [
    "group:runtime", "group:web", "group:ui", "group:automation", "group:messaging",
    "group:nodes", "group:agents", "group:memory", "group:media", "group:sessions",
    "group:plugins", "render_ui",
]
ALLOW = ["read", "write", "edit", "apply_patch"]


def safe_config(write_dir):
    return {"config": {"agents": {"list": [{
        "id": "agent-d4-exec-002",
        "workspace": write_dir,
        "sandbox": {
            "mode": "all", "backend": "docker", "scope": "agent", "workspaceAccess": "rw",
            "docker": {"image": "openclaw-sandbox:bookworm-slim", "network": "none",
                       "readOnlyRoot": True, "binds": [write_dir + ":/d4run:rw"]},
        },
        "tools": {"allow": ALLOW, "deny": DENY, "exec": {"mode": "deny"},
                  "fs": {"workspaceOnly": True},
                  "sandbox": {"tools": {"allow": ALLOW, "deny": DENY}},
                  "elevated": {"enabled": False}},
        "skills": [],
    }]}}}


class GatewayCalls:
    def __init__(self, config, checker_unavailable=False):
        self.config = config
        self.checker_unavailable = checker_unavailable
        self.methods = []

    def __call__(self, argv, **kwargs):
        if len(argv) > 1 and argv[1].endswith("d4_isolation_check.js"):
            if self.checker_unavailable:
                raise FileNotFoundError("checker unavailable")
            return REAL_RUN(argv, **kwargs)
        method = argv[3]
        self.methods.append(method)
        if method == "config.get":
            value = self.config
        elif method == "agent":
            params = json.loads(argv[5])
            value = {"runId": params["idempotencyKey"], "status": "accepted",
                     "sessionKey": params["sessionKey"]}
        else:
            raise AssertionError("unexpected method: %s" % method)
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(value), stderr="")


@unittest.skipUnless(shutil.which("node"), "Node is required for the shared D4 gate")
class D4RealEntryGateTests(D4CtlTestCase):
    BIN = "/usr/bin/openclaw-fixture"
    STATE = "/tmp/openclaw-state-fixture"

    def setUp(self):
        super().setUp()
        self.write_dir = str(Path(self.workspace, ".conductor-local/d4-real-chain-validation"))
        Path(self.write_dir).mkdir(parents=True, exist_ok=True)

    def real_args(self, cmd, task_id):
        return [cmd[0], cmd[1], "--task", task_id,
                "--gateway-bin", self.BIN, "--gateway-state-dir", self.STATE]

    def test_submit_checks_config_twice_then_calls_agent(self):
        _, created = self.create_task()
        calls = GatewayCalls(safe_config(self.write_dir))
        with mock.patch("adapters.qclaw_gateway.subprocess.run", side_effect=calls):
            code, result = self.cli(self.real_args(("attempt", "submit"),
                                                   created["data"]["taskId"]), synthetic=False)
        self.assertEqual(code, 0, result)
        self.assertEqual(calls.methods, ["config.get", "config.get", "agent"])
        self.assertEqual(result["data"]["submission"]["status"], "accepted_by_host")

    def test_unsafe_config_rejected_before_attempt_or_agent_rpc(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        config = safe_config(self.write_dir)
        config["config"]["agents"]["list"][0]["sandbox"]["docker"]["network"] = "bridge"
        calls = GatewayCalls(config)
        with mock.patch("adapters.qclaw_gateway.subprocess.run", side_effect=calls):
            code, result = self.cli(self.real_args(("attempt", "submit"), task_id),
                                    synthetic=False)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(calls.methods, ["config.get"])
        store = SqliteStore(self.db)
        self.assertEqual(store.list_attempts(task_id), [])
        store.close()

    def test_checker_failure_rejected_without_agent_rpc(self):
        _, created = self.create_task()
        calls = GatewayCalls(safe_config(self.write_dir), checker_unavailable=True)
        with mock.patch("adapters.qclaw_gateway.subprocess.run", side_effect=calls):
            code, result = self.cli(self.real_args(("attempt", "submit"),
                                                   created["data"]["taskId"]), synthetic=False)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(calls.methods, ["config.get"])

    def test_relay_checks_config_before_agent_rpc_and_retains_pending_on_failure(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        submitted = self.cli(["attempt", "submit", "--task", task_id],
                             transport=ResponsiveTransport())[1]
        attempt_id = submitted["data"]["attemptId"]
        self.ingest_events(task_id, attempt_id, [
            event_payload(task_id, attempt_id, "task.started", 1),
            delivered_payload(task_id, attempt_id, 2),
        ])
        store = SqliteStore(self.db)
        delivery_id = store.list_deliveries(task_id)[-1]["delivery_id"]
        store.close()
        review_file = self.write_json("review.json", {
            "decision": "changes_requested", "deliveryId": delivery_id, "note": "返工",
        })
        reviewed = self.cli(["review", "submit", "--task", task_id,
                             "--file", review_file])[1]
        self.assertTrue(reviewed["ok"], reviewed)

        config = safe_config(self.write_dir)
        config["config"]["agents"]["list"][0]["tools"]["exec"]["mode"] = "allow"
        blocked = GatewayCalls(config)
        with mock.patch("adapters.qclaw_gateway.subprocess.run", side_effect=blocked):
            code, result = self.cli(self.real_args(("review", "relay"), task_id),
                                    synthetic=False)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(blocked.methods, ["config.get"])

        allowed = GatewayCalls(safe_config(self.write_dir))
        with mock.patch("adapters.qclaw_gateway.subprocess.run", side_effect=allowed):
            code, result = self.cli(self.real_args(("review", "relay"), task_id),
                                    synthetic=False)
        self.assertEqual(code, 0, result)
        self.assertEqual(allowed.methods, ["config.get", "config.get", "agent"])

    def test_direct_adapter_cannot_bypass_gate_with_wrong_key(self):
        transport = CliGatewayTransport([self.BIN], self.STATE)
        with self.assertRaises(GatewayTransportError):
            transport.call("agent", {"agentId": "agent-d4-exec-002",
                                     "idempotencyKey": "unapproved"}, 100)


if __name__ == "__main__":
    unittest.main()
