"""离线权限矩阵、真实 hook 子进程及 QA 路由；无宿主/provider/业务调用。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from adapters import task_permissions as policy_module
from adapters.task_permissions import build_policy, check_tool, cli_arguments
from adapters.workbuddy import CodeBuddyTransport, workbuddy_binding
from adapters.codebuddy_cli import IndependentCodeBuddyTransport, read_binding
from scripts import d4ctl, d4_workbuddy_run as runner
from storage.sqlite_store import SqliteStore


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "execution"
        self.workspace.mkdir()
        self.input = self.root / "candidate"
        self.input.mkdir()
        (self.input / "src.py").write_text("frozen source")
        self.permissions = {"writeScope": ["receipts", "qa-output", "tests"],
            "readScope": [str(self.input)], "bashCommands": ["python3 -m unittest discover -s tests"],
            "allowNetwork": False, "allowBusinessWrite": False, "allowGit": False, "allowPublish": False}

    def tearDown(self):
        self.tmp.cleanup()

    def policy(self, agent="qa-wkb1", **changes):
        return build_policy(agent, str(self.workspace), {**self.permissions, **changes}, allowed_roots=[str(self.root)])

    def event(self, tool, path=None, **inputs):
        if path is not None:
            inputs["file_path"] = str(path)
        return {"tool_name": tool, "tool_input": inputs, "cwd": str(self.workspace)}

    def test_qa_can_write_reports_tests_and_read_frozen_input(self):
        for agent in ("qa-wkb1", "qa-ckb1"):
            policy = self.policy(agent)
            self.assertIsNone(check_tool(policy, self.event("Read", self.input / "src.py")))
            for path in ("qa-output/report.md", "tests/test_fixture.py", "receipts/events.json"):
                self.assertIsNone(check_tool(policy, self.event("Write", self.workspace / path)))
            self.assertEqual(check_tool(policy, self.event("Edit", self.workspace / "qa-output/report.md")), "TASK_TOOL_NOT_ALLOWED")
            self.assertEqual(check_tool(policy, self.event("Write", self.input / "src.py")), "TASK_PATH_NOT_ALLOWLISTED")
            self.assertEqual(check_tool(policy, self.event("Write", self.workspace / "product.py")), "TASK_PATH_NOT_ALLOWLISTED")

    def test_dev_can_edit_only_its_new_candidate(self):
        for agent in ("dev-wkb1", "dev-ckb1"):
            policy = self.policy(agent, writeScope=["new-candidate", "receipts"])
            self.assertIsNone(check_tool(policy, self.event("Edit", self.workspace / "new-candidate/src.py")))
            self.assertEqual(check_tool(policy, self.event("Write", self.input / "src.py")), "TASK_PATH_NOT_ALLOWLISTED")

    def test_symlink_escape_and_unlisted_reads_denied(self):
        policy = self.policy()
        (self.workspace / "qa-output").symlink_to(self.input, target_is_directory=True)
        self.assertEqual(check_tool(policy, self.event("Write", self.workspace / "qa-output/src.py")), "TASK_PATH_NOT_ALLOWLISTED")
        with self.assertRaises(Exception):
            self.policy()
        self.assertEqual(check_tool(policy, self.event("Read", self.root / "unlisted")), "TASK_PATH_NOT_ALLOWLISTED")

    def test_exact_bash_only_with_fixed_cwd(self):
        policy = self.policy()
        allowed = self.event("Bash", command=self.permissions["bashCommands"][0])
        self.assertIsNone(check_tool(policy, allowed))
        self.assertEqual(check_tool(policy, {**allowed, "cwd": str(self.input)}), "TASK_BASH_CWD_MISMATCH")
        for command in ("git push", "npm install", "python3 -c 'print(1)'", "python3 -m unittest; git push"):
            self.assertEqual(check_tool(policy, self.event("Bash", command=command)), "TASK_BASH_NOT_ALLOWLISTED")
            with self.assertRaises(ValueError):
                self.policy(bashCommands=[command])
        self.assertEqual(check_tool(self.policy(bashCommands=[]), allowed), "TASK_TOOL_NOT_ALLOWED")

    def test_bash_tool_is_not_exposed_without_explicit_command(self):
        policy = self.policy(bashCommands=[])
        self.assertNotIn("Bash", policy["tools"])
        args = cli_arguments(policy, self.root / "runtime/no-bash-policy.json")
        self.assertNotIn("Bash", args[args.index("--tools") + 1].split(","))
        allowed = json.loads(args[args.index("--settings") + 1])["permissions"]["allow"]
        self.assertFalse(any(item.startswith("Bash(") for item in allowed))
        self.assertEqual(check_tool(policy, self.event("Bash", command="python3 -m unittest discover -s tests")),
                         "TASK_TOOL_NOT_ALLOWED")

    def test_high_risk_unknown_and_whole_qa_workspace_fail_closed(self):
        for key in ("allowNetwork", "allowBusinessWrite", "allowGit", "allowPublish"):
            with self.assertRaises(ValueError):
                self.policy(**{key: True})
        with self.assertRaises(ValueError):
            self.policy("unknown")
        with self.assertRaises(ValueError):
            self.policy(writeScope=["."])
        with self.assertRaises(Exception):
            self.policy(readScope=["/outside-controlled-root"])
        for entry in ("reports/*", "reports) Bash(*)", "reports\nsecret"):
            with self.assertRaises(ValueError):
                self.policy(writeScope=[entry])
        with self.assertRaisesRegex(ValueError, "INPUT_OUTPUT_SCOPE_OVERLAP"):
            self.policy(readScope=[str(self.workspace / "tests")])

    def test_current_receipt_only_and_malformed_tool_event_denied(self):
        policy = build_policy("qa-wkb1", str(self.workspace), self.permissions,
                              task_id="task-new", attempt_id="att-new")
        self.assertIsNone(check_tool(policy, self.event("Write", self.workspace / "receipts/task-new/att-new/report.md")))
        for path in ("receipts/task-old/att-old/events.json", "receipts/task-new/att-old/report.md"):
            self.assertEqual(check_tool(policy, self.event("Write", self.workspace / path)), "TASK_OLD_RECEIPT_WRITE_DENIED")
        for event in ([], {"tool_name": "Read", "tool_input": []}):
            self.assertEqual(check_tool(policy, event), "TASK_TOOL_EVENT_INVALID")

    def test_real_hook_exit_codes_and_permission_argv(self):
        path = self.root / "runtime/policy.json"
        args = cli_arguments(self.policy(), path)
        self.assertNotIn("bypassPermissions", args)
        settings = json.loads(args[args.index("--settings") + 1])
        self.assertTrue(settings["hooks"]["PreToolUse"])
        for event, code in [(self.event("Write", self.workspace / "qa-output/report.md"), 0),
                            (self.event("Write", self.input / "src.py"), 2),
                            (self.event("Edit", self.workspace / "qa-output/report.md"), 2),
                            (self.event("Bash", command="git push"), 2)]:
            run = subprocess.run([sys.executable, policy_module.__file__, str(path)], input=json.dumps(event),
                                 text=True, capture_output=True, cwd=self.workspace)
            self.assertEqual(run.returncode, code, run.stderr)
        self.assertEqual((self.input / "src.py").read_text(), "frozen source")

    def test_workbuddy_real_bindings_and_task_identity(self):
        for agent in ("dev-wkb1", "qa-wkb1"):
            binding = workbuddy_binding(agent)
            self.assertEqual(binding["hostAgent"], agent)
            self.assertEqual("Edit" in binding["disallowedTools"], agent.startswith("qa-"))
        transport = CodeBuddyTransport([sys.executable], self.workspace, self.root / "store",
            env={"HOME": str(self.root)}, binding={"hostAgent": "dev-wkb1"})
        with self.assertRaisesRegex(ValueError, "BINDING_MISMATCH"):
            transport.configure_task({"agent_id": "qa-wkb1", "workspace": str(self.workspace)})

    def test_codebuddy_v2_uses_actual_member_and_complete_scope(self):
        root = Path(__file__).resolve().parents[3]
        for agent in ("dev-ckb1", "qa-ckb1"):
            connector = root / ".conductor-local/permissions-qa-dev-20261009/connectors" / (agent + ".json")
            self.assertEqual(read_binding(connector)["agentId"], agent)
            transport = IndependentCodeBuddyTransport(connector, self.workspace, self.root / agent)
            transport.configure_task({"agent_id": agent, "workspace": str(self.workspace),
                                      "permissions_json": json.dumps(self.permissions)})
            args = transport.argv("att-test", "fixture")
            self.assertEqual(args[args.index("--agent") + 1], agent)
            self.assertEqual("Edit" in args[args.index("--tools") + 1].split(","), agent.startswith("dev-"))
            allow = json.loads(args[args.index("--settings") + 1])["permissions"]["allow"]
            self.assertIn("Read(%s/**)" % self.input, allow)
            self.assertIn("Write(%s/qa-output/**)" % self.workspace, allow)
            self.assertIn("Bash(%s)" % self.permissions["bashCommands"][0], allow)

    def test_qa_can_be_created_and_runner_freezes_qa_identity(self):
        db = self.root / "db.sqlite"
        payload = {"projectId": "synthetic", "title": "QA fixture", "agentId": "qa-wkb1",
                   "sourceId": "workbuddy-codebuddy", "workspace": str(self.workspace),
                   "goal": "offline QA", "permissions": self.permissions}
        card = self.root / "task.json"
        card.write_text(json.dumps(payload))
        code, result = d4ctl.run(["--db", str(db), "--allowed-root", str(self.root),
            "--source", "workbuddy-codebuddy", "--agent", "qa-wkb1", "task", "create", "--file", str(card)])
        self.assertEqual(code, 0, result)
        task_id = result["data"]["taskId"]
        model_policy = {"schemaVersion": "d4-model-policy.v1", "primary": "deepseek-v4.1-flash",
            "allowedModels": ["deepseek-v4.1-flash"], "effectiveAt": "2026-01-01T00:00:00+00:00",
            "authorizedAt": "2026-01-01T00:00:00+00:00", "transparentFallback": False,
            "allowedAgents": ["qa-wkb1"], "allowedSources": ["workbuddy-codebuddy"], "allowedRoots": [str(self.root)]}
        workflow = self.root / "workflow.json"
        workflow.write_text(json.dumps({"schemaVersion": "d4-workflow.v1", "db": str(db),
            "taskId": task_id, "allowedAgents": ["qa-wkb1"], "allowedSource": "workbuddy-codebuddy",
            "modelPolicy": model_policy}))
        binary = self.root / "fixture-cli"
        binary.write_text("never executed")
        binary.chmod(0o700)
        args = argparse.Namespace(db=db, allowed_root=self.root, home=self.root, binary=binary,
            runtime_dir=self.root / "runtime", workflow=workflow, task=task_id,
            timeout_ms=1000, receipt_wait_seconds=0)
        with patch.object(runner.subprocess, "Popen") as launch:
            launch.return_value.pid = 123
            config = runner.start(args)
        self.assertEqual(config["agentId"], "qa-wkb1")
        runner._verify_policy_for_execution(config)
        with patch.object(runner.d4ctl, "run", return_value=(1, {})) as submit:
            self.assertEqual(runner.execute(args.runtime_dir), 1)
        submitted = submit.call_args.args[0]
        self.assertEqual(submitted[submitted.index("--agent") + 1], "qa-wkb1")
        with self.assertRaisesRegex(ValueError, "ALLOWED_AGENTS"):
            runner.validate_workflow_binding(json.loads(workflow.read_text()), db, task_id, "dev-wkb1")


if __name__ == "__main__":
    unittest.main()
