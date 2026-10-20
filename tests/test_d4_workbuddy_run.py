"""Persistent runner tests; local fixtures only, no real host/model requests."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile
import subprocess
import sys
import time
import unittest
from unittest import mock

from scripts import d4ctl, d4_workbuddy_run as runner
from storage.sqlite_store import SqliteStore
from test_d4_workbuddy_adapter import FIXTURE_SOURCE


_MISSING = object()  # 显式哨兵：区分“字段缺失”与“字段值为 None / 其他”。


class WorkBuddyRunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.directory = self.root / "runtime"
        self.home = self.root / "home"
        self.home.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.db = self.root / "d4.sqlite"
        self.binary = self.root / "fixture"
        self.binary.write_text("#!/bin/sh\nexit 0\n")
        self.binary.chmod(0o700)
        self.binding = {"hostAgent": "dev-wkb1", "pluginDir": "/fixture", "definitionSha256": "fixture"}
        payload = self.root / "task.json"
        payload.write_text(json.dumps({"projectId": "d4-runner", "title": "fixture",
            "agentId": "dev-wkb1", "sourceId": "workbuddy-codebuddy",
            "workspace": str(self.workspace), "goal": "fixture",
            "permissions": {"writeScope": ["."], "allowNetwork": False,
                "allowBusinessWrite": False, "allowGit": False, "allowPublish": False}}))
        code, created = d4ctl.run(["--db", str(self.db), "--allowed-root", str(self.root),
            "--source", "workbuddy-codebuddy", "--agent", "dev-wkb1",
            "task", "create", "--file", str(payload)])
        self.assertEqual(code, 0, created)
        self.task_id = created["data"]["taskId"]
        # Synthetic workflow fixture：显式 modelPolicy，仅本地合成数据，非真实用户授权。
        self.policy = {"schemaVersion": "d4-model-policy.v1",
            "primary": "deepseek-v4.1-flash",
            "allowedModels": ["deepseek-v4.1-flash", "hy4-preview", "glm-5.3-flash"],
            "effectiveAt": "2026-10-01T00:00:00+00:00",
            "authorizedAt": "2026-10-01T00:00:00+00:00",
            "transparentFallback": False,
            "allowedAgents": ["dev-wkb1"], "allowedSources": ["workbuddy-codebuddy"],
            "allowedRoots": [str(self.root)]}
        self.workflow = self.root / "workflow.json"
        self.write_workflow()

    def write_workflow(self, policy=None, **overrides):
        document = {"schemaVersion": "d4-workflow.v1", "taskId": self.task_id,
            "automationId": "auto-fixture", "ownerThreadId": "thread-fixture",
            "runtimeDir": str(self.directory), "db": str(self.db),
            "allowedAgents": ["dev-wkb1"], "allowedSource": "workbuddy-codebuddy",
            "modelPolicy": policy or self.policy}
        document.update(overrides)
        self.workflow.write_text(json.dumps(document))
        return document

    def tearDown(self):
        self.tmp.cleanup()

    def _fresh_task(self, suffix):
        """新建独立任务 + 工作区 + runtime 目录（本地合成 fixture，非真实业务数据）。"""
        workspace = self.root / ("workspace-" + suffix)
        workspace.mkdir()
        payload = self.root / ("task-%s.json" % suffix)
        payload.write_text(json.dumps({"projectId": "d4-runner", "title": "fixture",
            "agentId": "dev-wkb1", "sourceId": "workbuddy-codebuddy",
            "workspace": str(workspace), "goal": "fixture",
            "permissions": {"writeScope": ["."], "allowNetwork": False,
                "allowBusinessWrite": False, "allowGit": False, "allowPublish": False}}))
        code, created = d4ctl.run(["--db", str(self.db), "--allowed-root", str(self.root),
            "--source", "workbuddy-codebuddy", "--agent", "dev-wkb1",
            "task", "create", "--file", str(payload)])
        self.assertEqual(code, 0, created)
        self.task_id = created["data"]["taskId"]
        self.workspace = workspace
        self.directory = self.root / ("runtime-" + suffix)
        self.write_workflow()
        return created

    def _runtime_diag(self):
        """失败诊断：读回 runtime 现场 state/result/config，不改变任何文件。"""
        diag = {}
        for name in ("state.json", "result.json", "config.json"):
            path = self.directory / name
            if path.is_file():
                diag[name] = path.read_text()
        return json.dumps(diag, ensure_ascii=False)

    def start(self, model=None):
        args = argparse.Namespace(db=self.db, allowed_root=self.root, home=self.home,
            binary=self.binary, runtime_dir=self.directory, task=self.task_id,
            workflow=self.workflow, timeout_ms=1000, receipt_wait_seconds=0)
        if model is not None:
            args.model = model
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            popen.return_value.pid = 12345
            result = runner.start(args)
            call = popen.call_args
            self.assertTrue(call.kwargs["start_new_session"])
            self.assertNotIn("--bg", call.args[0])
        return result

    def test_explicit_model_is_frozen_and_forwarded_without_fallback(self):
        for model in ("hy4-preview", "glm-5.3-flash"):
            with self.subTest(model=model):
                self.directory = self.root / model
                # Mock creation for each local model fixture; no real host is started.
                original_task = self.task_id
                with mock.patch.object(runner.TaskService, "create_attempt", return_value={"attemptId": "att-fixture"}):
                    result = self.start(model)
                self.assertEqual(result["model"], model)
                with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                        mock.patch.object(runner.d4ctl, "run", return_value=(1, {"ok": False})) as submit, \
                        mock.patch.object(SqliteStore, "get_attempt",
                            return_value={"attempt_id": "att-fixture", "task_id": self.task_id}):
                    # create_attempt 被 mock，故显式合成 attempt 供 read_only 归属复核。
                    self.assertEqual(runner.execute(self.directory), 1)
                    argv = submit.call_args.args[0]
                    self.assertEqual(argv[argv.index("--workbuddy-model") + 1], model)
                    self.assertNotIn("--fallback-model", argv)
                self.assertEqual(self.task_id, original_task)

    def test_invalid_model_refused_before_attempt_runtime_or_process(self):
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            args = argparse.Namespace(model="auto")
            with self.assertRaisesRegex(ValueError, "WORKBUDDY_MODEL_NOT_ALLOWED"):
                runner.start(args)
            create.assert_not_called()
            popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_custom_glm_policy_requires_exact_authorized_id(self):
        model = "custom-local:glm-5.3-flash"
        policy = dict(self.policy, primary=model, allowedModels=[model])
        self.write_workflow(policy)
        result = self.start(model)
        self.assertEqual(result["model"], model)
        self.assertEqual(result["modelPolicy"]["allowedModels"], [model])
        for denied in ("glm-5.3-flash", "hy4-preview", "deepseek-v4.1-flash"):
            with self.subTest(model=denied):
                with self.assertRaisesRegex(ValueError, "MODEL_NOT_AUTHORIZED"):
                    runner.validate_model_policy(policy, denied)

    def test_start_freezes_model_binding_and_attempt(self):
        result = self.start()
        self.assertEqual(result["model"], "deepseek-v4.1-flash")
        self.assertEqual(result["binding"], self.binding)
        store = SqliteStore(self.db, read_only=True)
        self.assertEqual(store.count_attempts(self.task_id), 1)
        self.assertEqual(store.get_attempt(result["attemptId"])["submit_state"], "created")
        store.close()
        with self.assertRaisesRegex(ValueError, "已有轮次"):
            self.start()
        config = json.loads((self.directory / "config.json").read_text())
        self.assertEqual(config["workflowPath"], str(self.workflow.resolve()))
        self.assertEqual(config["modelPolicySha256"],
                         runner.sha256_text(runner._canonical_json(self.policy)))
        self.assertEqual(config["modelPolicy"], self.policy)

    def test_start_refuses_when_workflow_file_missing_or_invalid(self):
        self.workflow.unlink()
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "workflow"):
                self.start()
        create.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_start_refuses_task_binding_mismatch(self):
        document = self.write_workflow()
        document["taskId"] = "task-other"
        self.workflow.write_text(json.dumps(document))
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "WORKFLOW_TASK_BINDING_MISMATCH"):
                self.start()
        create.assert_not_called()
        popen.assert_not_called()

    def test_start_refuses_model_not_in_policy_allowed_models(self):
        policy = dict(self.policy, allowedModels=["glm-5.3-flash"])
        self.write_workflow(policy)
        self.directory = self.root / "policy-narrow"
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            args = argparse.Namespace(db=self.db, allowed_root=self.root, home=self.home,
                binary=self.binary, runtime_dir=self.directory, task=self.task_id,
                workflow=self.workflow, model="deepseek-v4.1-flash",
                timeout_ms=1000, receipt_wait_seconds=0)
            with self.assertRaisesRegex(ValueError, "MODEL_NOT_AUTHORIZED"):
                runner.start(args)
        create.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_start_refuses_future_effective_policy(self):
        policy = dict(self.policy, effectiveAt="2999-01-01T00:00:00+00:00")
        self.write_workflow(policy)
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "MODEL_POLICY_FUTURE_EFFECTIVE"):
                self.start()
        create.assert_not_called()
        popen.assert_not_called()

    def test_start_refuses_transparent_fallback_enabled(self):
        policy = dict(self.policy, transparentFallback=True)
        self.write_workflow(policy)
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "MODEL_POLICY_FALLBACK_NOT_DISABLED"):
                self.start()
        create.assert_not_called()
        popen.assert_not_called()

    def test_start_refuses_workspace_outside_policy_roots(self):
        outside = self.root / "outside"
        outside.mkdir()
        policy = dict(self.policy, allowedRoots=[str(outside)])
        self.write_workflow(policy)
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "TASK_WORKSPACE_OUTSIDE_ALLOWED_ROOTS"):
                self.start()
        create.assert_not_called()
        popen.assert_not_called()

    def test_execute_refuses_policy_drift_before_host_call(self):
        self.start()
        drifted = dict(self.policy, allowedModels=["glm-5.3-flash"])
        self.write_workflow(drifted)
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            # cox 裁定：execute 拒绝直接 return 1（不再抛异常），且不调用宿主 / 采集。
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
            importer.assert_not_called()
        self.assertFalse((self.directory / "started").exists())

    def test_legacy_runtime_collect_readable_but_reexecute_refused(self):
        config = self.start()
        config_path = self.directory / "config.json"
        legacy = json.loads(config_path.read_text())
        legacy.pop("modelPolicy")
        legacy.pop("modelPolicySha256")
        runner.write_json(config_path, legacy)
        # 旧 runtime 状态/采集继续可读。
        status = runner.inspect_run(self.directory)
        self.assertEqual(status["taskId"], self.task_id)
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            # 无 policy 的旧 runtime 禁止重新 execute：return 1 + 精确 message，不调用宿主。
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
            importer.assert_not_called()
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["status"], "attention_required")
        self.assertEqual(state["message"], "MODEL_POLICY_MISSING")
        self.assertFalse((self.directory / "started").exists())

    def test_orphan_state_is_unknown_and_query_does_not_modify_files(self):
        self.start()
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        result = runner.inspect_run(self.directory)
        self.assertFalse(result["runnerAlive"])
        self.assertEqual(result["state"]["status"], "execution_unknown")
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.directory.iterdir()})

    def test_live_lock_is_checked_without_killing_pid(self):
        self.start()
        fd = os.open(self.directory / "runner.lock", os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertTrue(runner.inspect_run(self.directory)["runnerAlive"])
        finally:
            os.close(fd)

    def test_execute_collects_and_never_executes_twice(self):
        self.start()
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run", return_value=(0, {"ok": True})) as submit, \
                mock.patch.object(runner, "collect_attempt", return_value={"status": "imported",
                    "taskStatus": "awaiting_review", "eventCount": 2}):
            self.assertEqual(runner.execute(self.directory), 0)
            self.assertEqual(submit.call_count, 1)
            with self.assertRaises(FileExistsError):
                runner.execute(self.directory)
            self.assertEqual(submit.call_count, 1)
        status = runner.inspect_run(self.directory)
        self.assertEqual(status["state"]["status"], "finished")
        self.assertEqual(status["state"]["collection"]["taskStatus"], "awaiting_review")

    def test_binding_drift_stops_before_submission(self):
        self.start()
        with mock.patch.object(runner, "workbuddy_binding", return_value={"different": True}), \
                mock.patch.object(runner.d4ctl, "run") as submit:
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
        self.assertEqual(runner.inspect_run(self.directory)["state"]["status"], "attention_required")

    def test_unknown_submission_never_collects_or_retries(self):
        self.start()
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run", return_value=(1,
                    {"ok": False, "error": {"code": "submit_unknown"}})) as submit, \
                mock.patch.object(runner, "collect_attempt") as collect:
            self.assertEqual(runner.execute(self.directory), 1)
            self.assertEqual(submit.call_count, 1)
            collect.assert_not_called()
        self.assertEqual(runner.inspect_run(self.directory)["result"]["exitCode"], 1)

    def test_receipt_missing_remains_attention_required(self):
        self.start()
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run", return_value=(0, {"ok": True})), \
                mock.patch.object(runner, "collect_attempt", return_value={"status": "waiting",
                                                                         "reason": "receipt_missing"}):
            self.assertEqual(runner.execute(self.directory), 1)
        self.assertEqual(runner.inspect_run(self.directory)["state"]["collection"]["status"], "waiting")

    def test_collection_error_preserves_submission_result(self):
        self.start()
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run", return_value=(0, {"ok": True})), \
                mock.patch.object(runner, "collect_attempt", side_effect=ValueError("receipt_invalid")):
            self.assertEqual(runner.execute(self.directory), 1)
        result = runner.inspect_run(self.directory)
        self.assertEqual(result["result"]["exitCode"], 0)
        self.assertEqual(result["state"]["message"], "receipt_invalid")

    def test_no_host_acceptance_no_receipt_import(self):
        config = self.start()
        with mock.patch.object(runner, "import_receipt") as importer:
            self.assertEqual(runner.collect_attempt(config)["reason"], "host_unconfirmed")
            importer.assert_not_called()

    def test_collection_recovery_never_submits_or_creates_another_attempt(self):
        self.start()
        with mock.patch.object(runner, "collect_attempt", return_value={"status": "imported",
                "taskStatus": "awaiting_review"}), mock.patch.object(runner.d4ctl, "run") as submit:
            self.assertEqual(runner.recover_collection(self.directory)["status"], "imported")
            submit.assert_not_called()
        store = SqliteStore(self.db, read_only=True)
        self.assertEqual(store.count_attempts(self.task_id), 1)
        store.close()
        self.assertEqual(runner.inspect_run(self.directory)["state"]["recovery"], "collection_only")

    def test_runtime_symlink_rejected(self):
        self.start()
        link = self.root / "linked"
        link.symlink_to(self.directory)
        with self.assertRaisesRegex(ValueError, "符号链接"):
            runner.inspect_run(link)

    def test_detached_process_executes_collects_and_readback_survives_exit(self):
        self.start()
        # A separate Python process runs the production supervisor against a local fake CLI.
        fixture = self.root / "cli-fixture.py"
        source = FIXTURE_SOURCE.replace('emit({"type": "result", "subtype": "success",', '''
    prompt = sys.argv[-1]
    bundle = json.loads(prompt.split("最终 JSON：", 1)[1])
    paths = bundle["events"][1]["payload"]
    for relative in paths["changedFiles"]:
        path = os.path.join(os.getcwd(), relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("local fixture report")
    path = os.path.join(os.path.dirname(os.path.join(os.getcwd(), paths["reportPath"])), "events.json")
    with open(path, "w") as handle:
        json.dump(bundle, handle)
    emit({"type": "result", "subtype": "success",''')
        fixture.write_text("#!" + sys.executable + "\n" + source)
        fixture.chmod(0o700)
        config_path = self.directory / "config.json"
        config = json.loads(config_path.read_text())
        config["binary"] = str(fixture)
        runner.write_json(config_path, config)
        launch_code = """
import json, sys
from pathlib import Path
from scripts import d4_workbuddy_run as runner, d4ctl
directory = Path(sys.argv[1])
binding = json.loads((directory / 'config.json').read_text())['binding']
runner.workbuddy_binding = lambda agent: binding
d4ctl.workbuddy_binding = lambda agent: binding
raise SystemExit(runner.execute(directory))
"""
        process = subprocess.Popen([sys.executable, "-B", "-c", launch_code, str(self.directory)],
                                   cwd=str(Path(runner.__file__).resolve().parents[1]),
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            self.fail("detached fixture did not stop: %s" % self._runtime_diag())
        # 失败时只追加 state/result 现场诊断；不延长 sleep，不移除真实 fake-CLI 链路。
        self.assertEqual(process.returncode, 0,
                         "%s\n%s" % (stderr.decode(), self._runtime_diag()))
        status = runner.inspect_run(self.directory)
        self.assertFalse(status["runnerAlive"], self._runtime_diag())
        self.assertEqual(status["state"]["status"], "finished", self._runtime_diag())
        self.assertEqual(status["state"]["collection"]["eventCount"], 2, self._runtime_diag())
        self.assertEqual(status["hostRuntime"]["observedModel"], "deepseek-v4.1-flash",
                         self._runtime_diag())
        store = SqliteStore(self.db, read_only=True)
        self.assertEqual(store.get_task(self.task_id)["status"], "awaiting_review",
                         self._runtime_diag())
        self.assertEqual(store.count_attempts(self.task_id), 1, self._runtime_diag())
        store.close()

    def test_guard_stops_cli_when_owner_pipe_closes(self):
        guard = Path(runner.__file__).with_name("d4_workbuddy_guard.py")
        read_fd, write_fd = os.pipe()
        pid_path = self.root / "child.pid"
        program = "import os,time;open(%r,'w').write(str(os.getpid()));time.sleep(30)" % str(pid_path)
        process = subprocess.Popen([sys.executable, "-B", str(guard), "--owner-fd", str(read_fd),
                                   "--timeout", "10", "--", sys.executable, "-c", program],
                                  pass_fds=(read_fd,), start_new_session=True,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        os.close(read_fd)
        try:
            deadline = time.monotonic() + 3
            while not pid_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_path.exists())
            child_pid = int(pid_path.read_text())
            os.close(write_fd)
            write_fd = -1
            process.wait(timeout=3)
            self.assertEqual(process.returncode, -9)
            # A zombie is no longer executing; ps must not show a live state.
            status = subprocess.run(["/bin/ps", "-p", str(child_pid), "-o", "stat="],
                                    capture_output=True, text=True).stdout.strip()
            self.assertTrue(not status or status.startswith("Z"), status)
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            if process.poll() is None:
                os.killpg(process.pid, 9)
                process.wait()

    def test_start_refuses_workspace_outside_allowed_root(self):
        narrow = self.root / "narrow-root"
        narrow.mkdir()
        self.directory = self.root / "narrow-runtime"
        args = argparse.Namespace(db=self.db, allowed_root=narrow, home=self.home,
            binary=self.binary, runtime_dir=self.directory, task=self.task_id,
            workflow=self.workflow, timeout_ms=1000, receipt_wait_seconds=0)
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "TASK_WORKSPACE_OUTSIDE_ALLOWED_ROOT"):
                runner.start(args)
        create.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_start_refuses_symlink_db_path(self):
        link = self.root / "linked.sqlite"
        link.symlink_to(self.db)
        args = argparse.Namespace(db=link, allowed_root=self.root, home=self.home,
            binary=self.binary, runtime_dir=self.directory, task=self.task_id,
            workflow=self.workflow, timeout_ms=1000, receipt_wait_seconds=0)
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "DB_PATH_INVALID"):
                runner.start(args)
        create.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_validate_policy_rejects_wrong_source(self):
        policy = dict(self.policy, allowedSources=["other-source"])
        with self.assertRaisesRegex(ValueError, "MODEL_POLICY_SOURCE_NOT_ALLOWED"):
            runner.validate_model_policy(policy, "deepseek-v4.1-flash")

    def test_validate_policy_rejects_wrong_agent(self):
        policy = dict(self.policy, allowedAgents=["other-agent"])
        with self.assertRaisesRegex(ValueError, "MODEL_POLICY_AGENT_NOT_ALLOWED"):
            runner.validate_model_policy(policy, "deepseek-v4.1-flash")

    def test_validate_policy_rejects_symlink_root(self):
        link = self.root / "root-link"
        link.symlink_to(self.root)
        policy = dict(self.policy, allowedRoots=[str(link)])
        with self.assertRaisesRegex(ValueError, "MODEL_POLICY_ROOTS_INVALID"):
            runner.validate_model_policy(policy, "deepseek-v4.1-flash")

    def test_start_refuses_nonempty_workflow_attempt_before_creating_round(self):
        document = self.write_workflow()
        document["attemptId"] = "att-old"
        self.workflow.write_text(json.dumps(document))
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "WORKFLOW_ATTEMPT_BINDING_MISMATCH"):
                self.start()
        create.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_execute_refuses_workflow_attempt_drift(self):
        self.start()
        document = self.write_workflow()
        document["attemptId"] = "att-other"
        self.workflow.write_text(json.dumps(document))
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
            importer.assert_not_called()
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["status"], "attention_required")
        self.assertEqual(state["message"], "WORKFLOW_ATTEMPT_BINDING_MISMATCH")
        self.assertFalse((self.directory / "started").exists())

    def test_execute_refuses_workflow_task_switch(self):
        self.start()
        document = self.write_workflow()
        document["taskId"] = "task-switched"
        self.workflow.write_text(json.dumps(document))
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
            importer.assert_not_called()
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["status"], "attention_required")
        self.assertEqual(state["message"], "WORKFLOW_TASK_BINDING_MISMATCH")
        self.assertFalse((self.directory / "started").exists())

    def test_execute_policy_drift_persists_attention_required(self):
        self.start()
        drifted = dict(self.policy, allowedModels=["glm-5.3-flash"])
        self.write_workflow(drifted)
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
            importer.assert_not_called()
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["status"], "attention_required")
        self.assertEqual(state["message"], "MODEL_POLICY_DRIFT")
        self.assertFalse((self.directory / "started").exists())

    # -- 本卡新增：workflow 顶层 db / agent / source 绑定与 attempt 归属 ----------

    def test_start_refuses_workflow_db_binding_missing_or_mismatched(self):
        cases = (("missing", _MISSING), ("mismatched", str(self.root / "other.sqlite")))
        for name, value in cases:
            with self.subTest(case=name):
                document = self.write_workflow()
                if value is _MISSING:
                    document.pop("db")
                else:
                    document["db"] = value
                self.workflow.write_text(json.dumps(document))
                self.directory = self.root / ("start-db-" + name)
                with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                        mock.patch.object(runner.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(ValueError, "WORKFLOW_DB_BINDING_MISMATCH"):
                        self.start()
                create.assert_not_called()
                popen.assert_not_called()
                self.assertFalse(self.directory.exists())

    def test_start_refuses_workflow_agent_binding_missing_or_mismatched(self):
        # 缺失 workflow.allowedAgents 时 modelPolicy.allowedAgents 仍含 dev-wkb1，
        # 必须仍拒绝：绝不以 modelPolicy 内字段替代 workflow 字段。
        cases = (("missing", _MISSING), ("mismatched", ["other-agent"]))
        for name, value in cases:
            with self.subTest(case=name):
                document = self.write_workflow()
                if value is _MISSING:
                    document.pop("allowedAgents")
                else:
                    document["allowedAgents"] = value
                self.workflow.write_text(json.dumps(document))
                self.directory = self.root / ("start-agent-" + name)
                with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                        mock.patch.object(runner.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(ValueError, "WORKFLOW_ALLOWED_AGENTS_INVALID"):
                        self.start()
                create.assert_not_called()
                popen.assert_not_called()
                self.assertFalse(self.directory.exists())

    def test_start_refuses_workflow_source_binding_missing_or_mismatched(self):
        cases = (("missing", _MISSING), ("mismatched", "other-source"))
        for name, value in cases:
            with self.subTest(case=name):
                document = self.write_workflow()
                if value is _MISSING:
                    document.pop("allowedSource")
                else:
                    document["allowedSource"] = value
                self.workflow.write_text(json.dumps(document))
                self.directory = self.root / ("start-source-" + name)
                with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                        mock.patch.object(runner.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(ValueError, "WORKFLOW_SOURCE_BINDING_MISMATCH"):
                        self.start()
                create.assert_not_called()
                popen.assert_not_called()
                self.assertFalse(self.directory.exists())

    def test_start_refuses_non_null_workflow_attempt_of_any_type(self):
        cases = (("empty-string", ""), ("false", False), ("zero", 0), ("text", "att-old"))
        for name, value in cases:
            with self.subTest(case=name):
                self.write_workflow(attemptId=value)
                self.directory = self.root / ("start-attempt-" + name)
                with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                        mock.patch.object(runner.subprocess, "Popen") as popen:
                    with self.assertRaisesRegex(ValueError, "WORKFLOW_ATTEMPT_BINDING_MISMATCH"):
                        self.start()
                create.assert_not_called()
                popen.assert_not_called()
                self.assertFalse(self.directory.exists())

    def test_start_accepts_absent_or_null_workflow_attempt(self):
        result = self.start()  # 默认 workflow 不含 attemptId（缺失）可启动。
        self.assertTrue(result["attemptId"])
        self._fresh_task("null-attempt")
        self.write_workflow(attemptId=None)  # 显式 null 亦可启动。
        result = self.start()
        self.assertTrue(result["attemptId"])

    def test_execute_refuses_workflow_db_agent_source_binding(self):
        cases = (
            ("db-mismatch", "db", str(self.root / "other.sqlite"), "WORKFLOW_DB_BINDING_MISMATCH"),
            ("db-missing", "db", _MISSING, "WORKFLOW_DB_BINDING_MISMATCH"),
            ("agent-mismatch", "allowedAgents", ["other-agent"], "WORKFLOW_ALLOWED_AGENTS_INVALID"),
            ("agent-missing", "allowedAgents", _MISSING, "WORKFLOW_ALLOWED_AGENTS_INVALID"),
            ("source-mismatch", "allowedSource", "other-source", "WORKFLOW_SOURCE_BINDING_MISMATCH"),
            ("source-missing", "allowedSource", _MISSING, "WORKFLOW_SOURCE_BINDING_MISMATCH"),
        )
        for name, field, value, code in cases:
            with self.subTest(case=name):
                self._fresh_task(name)
                self.start()
                document = json.loads(self.workflow.read_text())
                if value is _MISSING:
                    document.pop(field)
                else:
                    document[field] = value
                self.workflow.write_text(json.dumps(document))
                with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                        mock.patch.object(runner.d4ctl, "run") as submit, \
                        mock.patch.object(runner, "import_receipt") as importer:
                    self.assertEqual(runner.execute(self.directory), 1)
                    submit.assert_not_called()
                    importer.assert_not_called()
                state = json.loads((self.directory / "state.json").read_text())
                self.assertEqual(state["status"], "attention_required")
                self.assertEqual(state["message"], code)
                self.assertFalse((self.directory / "started").exists())

    def test_execute_refuses_attempt_missing_or_wrong_owner(self):
        for name in ("missing-attempt", "wrong-owner"):
            with self.subTest(case=name):
                self._fresh_task(name)
                self.start()
                frozen = json.loads((self.directory / "config.json").read_text())["attemptId"]
                # 显式合成 attempt 供 read_only get_attempt 复核；不削弱生产归属检查。
                synthetic = (None if name == "missing-attempt"
                             else {"attempt_id": frozen, "task_id": "task-other"})
                with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                        mock.patch.object(runner.d4ctl, "run") as submit, \
                        mock.patch.object(runner, "import_receipt") as importer, \
                        mock.patch.object(SqliteStore, "get_attempt", return_value=synthetic):
                    self.assertEqual(runner.execute(self.directory), 1)
                    submit.assert_not_called()
                    importer.assert_not_called()
                state = json.loads((self.directory / "state.json").read_text())
                self.assertEqual(state["status"], "attention_required")
                self.assertEqual(state["message"], "EXECUTION_ATTEMPT_BINDING_MISMATCH")
                self.assertFalse((self.directory / "started").exists())

    def test_duplicate_execute_keeps_terminal_files_and_ignores_drift(self):
        self.start()
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run", return_value=(0, {"ok": True})) as submit, \
                mock.patch.object(runner, "collect_attempt", return_value={"status": "imported",
                    "taskStatus": "awaiting_review", "eventCount": 2}):
            self.assertEqual(runner.execute(self.directory), 0)
            self.assertEqual(submit.call_count, 1)
        terminal = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(terminal["status"], "finished")
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        # 重复执行 + policy 漂移：先抛 FileExistsError，runtime 全文件逐字节不变。
        self.write_workflow(dict(self.policy, allowedModels=["glm-5.3-flash"]))
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit2, \
                mock.patch.object(runner, "import_receipt") as importer:
            with self.assertRaises(FileExistsError):
                runner.execute(self.directory)
            submit2.assert_not_called()
            importer.assert_not_called()
        after = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.assertEqual(before, after)
        self.assertEqual(json.loads((self.directory / "state.json").read_text())["status"], "finished")

    def test_duplicate_execute_ignores_missing_policy(self):
        self.start()
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run", return_value=(0, {"ok": True})), \
                mock.patch.object(runner, "collect_attempt", return_value={"status": "imported",
                    "taskStatus": "awaiting_review", "eventCount": 2}):
            self.assertEqual(runner.execute(self.directory), 0)
        config_path = self.directory / "config.json"
        legacy = json.loads(config_path.read_text())
        legacy.pop("modelPolicy")
        legacy.pop("modelPolicySha256")
        runner.write_json(config_path, legacy)
        before = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            with self.assertRaises(FileExistsError):
                runner.execute(self.directory)
            submit.assert_not_called()
            importer.assert_not_called()
        after = {path.name: path.read_bytes() for path in self.directory.iterdir()}
        self.assertEqual(before, after)

    def test_execute_refuses_attempt_binding_when_config_attempt_id_missing(self):
        # execute 只在 read_only get_attempt 找不到冻结 attempt 时拒绝；不写库、不迁移。
        self.start()
        config_path = self.directory / "config.json"
        config = json.loads(config_path.read_text())
        config["attemptId"] = "att-absent"
        runner.write_json(config_path, config)
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
            importer.assert_not_called()
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["message"], "EXECUTION_ATTEMPT_BINDING_MISMATCH")
        self.assertFalse((self.directory / "started").exists())

    # -- 本卡新增：start / execute 的 allowedRoot 真实路径门禁（symlink 及 symlink 祖先）---

    def test_start_refuses_symlink_allowed_root_before_runtime_attempt_or_popen(self):
        link = self.root / "start-root-link"
        link.symlink_to(self.root)
        args = argparse.Namespace(db=self.db, allowed_root=link, home=self.home,
            binary=self.binary, runtime_dir=self.directory, task=self.task_id,
            workflow=self.workflow, timeout_ms=1000, receipt_wait_seconds=0)
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "ALLOWED_ROOT_INVALID"):
                runner.start(args)
        create.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_start_refuses_symlink_ancestor_allowed_root_before_runtime_attempt_or_popen(self):
        realbase = self.root / "start-realbase"
        (realbase / "child").mkdir(parents=True)
        alink = self.root / "start-alink"
        alink.symlink_to(realbase)
        args = argparse.Namespace(db=self.db, allowed_root=alink / "child", home=self.home,
            binary=self.binary, runtime_dir=self.directory, task=self.task_id,
            workflow=self.workflow, timeout_ms=1000, receipt_wait_seconds=0)
        with mock.patch.object(runner.TaskService, "create_attempt") as create, \
                mock.patch.object(runner.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(ValueError, "ALLOWED_ROOT_INVALID"):
                runner.start(args)
        create.assert_not_called()
        popen.assert_not_called()
        self.assertFalse(self.directory.exists())

    def test_execute_refuses_symlink_allowed_root_before_started_submit_import(self):
        self.start()
        link = self.root / "exec-root-link"
        link.symlink_to(self.root)
        config_path = self.directory / "config.json"
        config = json.loads(config_path.read_text())
        config["allowedRoot"] = str(link)
        runner.write_json(config_path, config)
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
            importer.assert_not_called()
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["status"], "attention_required")
        self.assertEqual(state["message"], "ALLOWED_ROOT_INVALID")
        self.assertFalse((self.directory / "started").exists())

    def test_execute_refuses_symlink_ancestor_allowed_root_before_started_submit_import(self):
        self.start()
        realbase = self.root / "exec-realbase"
        (realbase / "child").mkdir(parents=True)
        alink = self.root / "exec-alink"
        alink.symlink_to(realbase)
        config_path = self.directory / "config.json"
        config = json.loads(config_path.read_text())
        config["allowedRoot"] = str(alink / "child")
        runner.write_json(config_path, config)
        with mock.patch.object(runner, "workbuddy_binding", return_value=self.binding), \
                mock.patch.object(runner.d4ctl, "run") as submit, \
                mock.patch.object(runner, "import_receipt") as importer:
            self.assertEqual(runner.execute(self.directory), 1)
            submit.assert_not_called()
            importer.assert_not_called()
        state = json.loads((self.directory / "state.json").read_text())
        self.assertEqual(state["status"], "attention_required")
        self.assertEqual(state["message"], "ALLOWED_ROOT_INVALID")
        self.assertFalse((self.directory / "started").exists())


if __name__ == "__main__":
    unittest.main()
