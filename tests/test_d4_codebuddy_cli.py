"""Offline independent-account routing tests; no provider requests."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import adapters.codebuddy_cli as independent

from adapters.codebuddy_cli import (
    AGENT_ID, MODEL_ID, SOURCE_ID, AUTH_HOME,
    IndependentCodeBuddyAdapter, IndependentCodeBuddyTransport, read_binding,
)
from adapters.workbuddy import WorkBuddyAdapter
from contracts.schemas import ContractError


class FixtureTransport:
    model = MODEL_ID
    binding = None
    workspace = ""

    def __init__(self, agent=AGENT_ID, model=MODEL_ID):
        self.calls = 0
        self.agent = agent
        self.observed_model = model

    def run(self, session_id, prompt):
        self.calls += 1
        events = [{"type": "system", "subtype": "init", "session_id": session_id,
                   "model": self.observed_model, "agent": self.agent},
                  {"type": "result", "subtype": "success", "is_error": False}]
        return {"events": events, "rawLines": [json.dumps(e) + "\n" for e in events],
                "exitCode": 0, "timedOut": False, "argv": []}


class OfflineHostProcess:
    """替代 subprocess.Popen 的离线假进程：只回放合成 stream-json，不启动宿主。"""

    def __init__(self, lines):
        # 不会被 killpg：returncode 已就绪，且看门狗在 finally 里先被取消。
        self.pid = 999999
        self.returncode = 0
        self.stdout = io.BytesIO("".join(lines).encode("utf-8"))
        self.stderr = io.BytesIO(b"")

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class OfflineHostFactory:
    """离线假 Popen 工厂：从真实 argv 取 --session-id，回放合成 stream-json。"""

    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.calls = 0

    def __call__(self, argv, **kwargs):
        session_id = argv[argv.index("--session-id") + 1]
        self.calls += 1
        events = [{"type": "system", "subtype": "init", "session_id": session_id,
                   "model": MODEL_ID, "agent": AGENT_ID, "cwd": str(self.workspace)},
                  {"type": "result", "subtype": "success", "is_error": False}]
        return OfflineHostProcess([json.dumps(event) + "\n" for event in events])


class IndependentRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.task = {"task_id": "task-ckb-test", "project_id": "d4-product",
                     "agent_id": AGENT_ID, "source_id": SOURCE_ID,
                     "workspace": str(self.root), "goal": "offline synthetic test",
                     "acceptance_json": "[]", "permissions_json": json.dumps({
                         "writeScope": [str(self.root)], "allowNetwork": False,
                         "allowBusinessWrite": False, "allowGit": False, "allowPublish": False})}
        self.attempt = {"task_id": self.task["task_id"], "attempt_id": "att-ckb-test",
                        "created_at": "2026-10-09T06:00:00+08:00"}

    def tearDown(self):
        self.tmp.cleanup()

    def test_source_and_replay(self):
        transport = FixtureTransport()
        adapter = IndependentCodeBuddyAdapter(transport, self.root / "store")
        self.assertEqual(adapter.submit(self.task, self.attempt).source_id, SOURCE_ID)
        adapter.submit(self.task, self.attempt)
        self.assertEqual(transport.calls, 1)
        self.assertEqual(adapter.get_status(self.attempt["attempt_id"])["runtimeIdentity"], "LOCAL_MATCH")

    def test_sources_and_agents_cannot_cross(self):
        IndependentCodeBuddyAdapter._validate_binding(self.task, self.attempt)
        with self.assertRaises(ContractError):
            WorkBuddyAdapter._validate_binding(self.task, self.attempt)
        for key, value in (("source_id", "workbuddy-codebuddy"), ("agent_id", "dev-wkb1")):
            with self.assertRaises(ContractError):
                IndependentCodeBuddyAdapter._validate_binding({**self.task, key: value}, self.attempt)

    def test_model_and_agent_mismatch_fail_closed(self):
        for index, transport in enumerate((FixtureTransport(model="other"), FixtureTransport(agent="dev-wkb1"))):
            adapter = IndependentCodeBuddyAdapter(transport, self.root / str(index))
            with self.assertRaises(ContractError):
                adapter.submit(self.task, self.attempt)
            self.assertEqual(adapter.get_status(self.attempt["attempt_id"])["hostState"], "unknown")

    def test_missing_agent_is_unverified(self):
        adapter = IndependentCodeBuddyAdapter(FixtureTransport(agent=None), self.root / "store")
        adapter.submit(self.task, self.attempt)
        self.assertEqual(adapter.get_status(self.attempt["attempt_id"])["runtimeIdentity"], "UNVERIFIED")

    def _connector_fixture(self, name):
        """合成一个离线 connector 部署；只写普通文件，不执行任何二进制、不联网。"""
        base = self.root / name
        binary = base / "cli-install/node_modules/@tencent-ai/codebuddy-code/bin/codebuddy"
        binary.parent.mkdir(parents=True)
        binary.write_text("synthetic binary, never executed")
        binary.chmod(0o700)
        bundle = binary.parent.parent / "dist/codebuddy-headless.js"
        bundle.parent.mkdir()
        bundle.write_text("synthetic bundle")
        (binary.parent.parent / "package.json").write_text('{"version":"2.163.0"}')
        home = base / "auth-home"
        home.mkdir(mode=0o700)
        (home / ".codebuddy").mkdir(mode=0o700)
        role = base / "dev-ckb1.md"
        role.write_text("synthetic role")
        workspace = base / "smoke"
        workspace.mkdir()
        connector = base / "connector.json"
        value = {"schemaVersion": "d4-codebuddy-connector.v1", "sourceId": SOURCE_ID,
                 "agentId": AGENT_ID, "environmentId": "codebuddy-cli-independent-local",
                 "model": MODEL_ID, "transparentFallback": False, "binary": str(binary),
                 "home": str(home), "configDir": str(home / ".codebuddy"), "version": "2.163.0",
                 "accountEvidence": "USER_CONFIRMED_INDEPENDENT_ACCOUNT", "rolePath": str(role)}
        value.update({k: hashlib.sha256(p.read_bytes()).hexdigest() for k, p in
                      (("binarySha256", binary), ("roleSha256", role), ("bundleSha256", bundle))})
        connector.write_text(json.dumps(value))
        return base, binary, home, workspace, connector

    def test_connector_argv_environment_and_drift(self):
        base, binary, home, workspace, connector = self._connector_fixture("deployment")
        with patch.multiple(independent, DEPLOYMENT_ROOT=base, BINARY=binary, AUTH_HOME=home):
            binding = read_binding(connector)
            transport = IndependentCodeBuddyTransport(connector, workspace, self.root / "host")
            with patch("adapters.codebuddy_cli.digest", return_value="drift"):
                with self.assertRaisesRegex(ValueError, "BINDING_DRIFT"):
                    read_binding(connector)
        args = transport.argv("att-new", "prompt")
        self.assertEqual(args[args.index("--model") + 1], MODEL_ID)
        self.assertEqual(args[args.index("--agent") + 1], AGENT_ID)
        self.assertEqual(args[args.index("--tools") + 1], "Read,Write")
        self.assertNotIn("--fallback-model", args)
        self.assertNotIn("--plugin-dir", args)
        self.assertEqual(transport.env["HOME"], str(home))
        self.assertEqual(transport.env["CODEBUDDY_CONFIG_DIR"], binding["configDir"])
        self.assertFalse(any("TOKEN" in key or "API_KEY" in key for key in transport.env))

    def test_connector_binding_is_preserved_in_runtime_and_receipt(self):
        """回归：初始化时不得丢 connector binding，运行事实必须固定独立账号身份。"""
        base, binary, home, workspace, connector = self._connector_fixture("binding-deployment")
        identity = ("agentId", "sourceId", "environmentId")
        expected = [AGENT_ID, SOURCE_ID, "codebuddy-cli-independent-local"]
        host = OfflineHostFactory(workspace)
        store = self.root / "binding-host"
        with patch.multiple(independent, DEPLOYMENT_ROOT=base, BINARY=binary, AUTH_HOME=home):
            binding = read_binding(connector)
            transport = IndependentCodeBuddyTransport(connector, workspace, store)
            self.assertEqual(transport.binding, binding)
            self.assertEqual([transport.binding[key] for key in identity], expected)
            # evidence-only：不得被当成 WorkBuddy 专家插件 binding 而改写 argv/校验。
            self.assertFalse(bool(transport.binding))
            with patch("adapters.workbuddy.subprocess.Popen", host):
                transport.run("att-bind-runtime", "prompt")
                runtime = json.loads((store / "attempts/att-bind-runtime/runtime.json").read_text())
                self.assertEqual(runtime["state"], "exited")
                self.assertEqual([runtime["binding"][key] for key in identity], expected)
                adapter = IndependentCodeBuddyAdapter(transport, self.root / "binding-adapter")
                attempt = {"task_id": self.task["task_id"], "attempt_id": "att-bind-receipt",
                           "created_at": self.attempt["created_at"]}
                adapter.submit({**self.task, "workspace": str(workspace)}, attempt)
                receipt = adapter.get_status(attempt["attempt_id"])
                self.assertEqual(receipt["hostState"], "completed")
                self.assertEqual(receipt["binding"], binding)
                self.assertEqual([receipt["binding"][key] for key in identity], expected)
        # 两次提交都在离线假进程内完成：未启动真实宿主、无网络、无业务写入。
        self.assertEqual(host.calls, 2)


if __name__ == "__main__":
    unittest.main()
