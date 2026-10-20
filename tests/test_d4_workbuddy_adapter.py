"""WorkBuddy adapter 定向测试（任务卡 D4-WB-BRIDGE-001 §6）。

覆盖：正常接收、重复提交只回放、迟到/无接收证据、协议损坏、超时、非零退出、
会话结束但无工具活动、产物路径越界（`..` / 绝对路径 / symlink / 不存在）、
来源与轮次绑定校验、权限模式与命令路径校验。

全部使用本地 fixture 进程模拟 ``codebuddy`` 的 stream-json 输出，
**不调用真实模型、不联网、不接触宿主配置**。
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from adapters.workbuddy import (
    ALLOWED_PERMISSION_MODES,
    CodeBuddyTransport,
    DEFAULT_WORKBUDDY_MODEL,
    WORKBUDDY_SOURCE_ID,
    WorkBuddyAdapter,
    WorkBuddyTransportError,
    parse_stream_line,
    safe_event,
    workbuddy_binding,
)
from contracts.schemas import ContractError
from scripts import d4ctl


FIXTURE_SOURCE = '''"""模拟 codebuddy CLI 的 stream-json 输出（测试用，不调用任何模型）。"""
import json
import os
import sys
import time


def arg(name, default=None):
    argv = sys.argv
    if name in argv:
        index = argv.index(name)
        if index + 1 < len(argv):
            return argv[index + 1]
    return default


sid = arg("--session-id", "unknown-session")
mode = os.environ.get("WB_FIXTURE_MODE", "ok")

# 真实执行计数：用于断言"重复提交不会产生第二次真实执行"。
with open(os.path.join(os.getcwd(), "run-count.txt"), "a") as handle:
    handle.write("run\\n")


def emit(payload):
    sys.stdout.write(json.dumps(payload) + "\\n")
    sys.stdout.flush()


if mode == "silent":
    # 宿主静默不输出：用于验证看门狗超时（readline 阻塞场景）。
    time.sleep(float(os.environ.get("WB_FIXTURE_SLEEP", "10")))
    sys.exit(0)

if mode != "no_init":
    emit({"type": "system", "subtype": "init",
          "session_id": ("wrong-session" if mode == "bad_session" else sid),
          "model": ("wrong-model" if mode == "bad_model" else arg("--model", "auto")),
          "agent": ("wrong-agent" if mode == "bad_agent" else arg("--agent")),
          "cwd": ("/wrong-workspace" if mode == "bad_cwd" else os.getcwd()),
          "permissionMode": "acceptEdits",
          "tools": ["Read", "Write", "Bash"]})

if mode == "bad_json":
    sys.stdout.write("this line is not JSON at all\\n")
    sys.stdout.flush()

if mode not in ("no_init", "no_result"):
    emit({"type": "system", "subtype": "status", "session_id": sid})
    if mode == "no_tool":
        # 复刻实测"假成功"：result=success，但一个工具都没动。
        emit({"type": "assistant", "session_id": sid, "message": {"content": [
            {"type": "text", "text": "did nothing"}]}})
    else:
        emit({"type": "assistant", "session_id": sid, "message": {"content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "result.md"}}]}})
        try:
            with open(os.path.join(os.getcwd(), "result.md"), "w") as handle:
                handle.write("WB_WRITE_OK")
        except OSError:
            pass
        emit({"type": "user", "session_id": sid, "message": {"content": [
            {"type": "tool_result", "is_error": False,
             "content": "Successfully created and wrote to new file: result.md"}]}})
        emit({"type": "assistant", "session_id": sid, "message": {"content": [
            {"type": "text", "text": "done"}]}})
    emit({"type": "result", "subtype": "success",
          "is_error": mode == "result_error", "result": "fixture result"})

if mode == "nonzero":
    sys.exit(3)
'''


def task_record(workspace, task_id="task-wb-1", source_id=WORKBUDDY_SOURCE_ID):
    return {
        "task_id": task_id,
        "project_id": "conductor",
        "agent_id": "dev-1",
        "source_id": source_id,
        "workspace": str(workspace),
        "goal": "WorkBuddy adapter 定向测试",
        "acceptance_json": json.dumps(["result.md 存在且内容正确"], ensure_ascii=False),
        "permissions_json": json.dumps(
            {"writeScope": ["."], "allowNetwork": False, "allowBusinessWrite": False,
             "allowGit": False, "allowPublish": False},
            ensure_ascii=False,
        ),
    }


def attempt_record(task_id="task-wb-1", attempt_id="att-wb-1"):
    return {"attempt_id": attempt_id, "task_id": task_id, "ordinal": 1, "state": "created",
            "created_at": "2026-10-01T12:00:00+08:00"}


class WorkBuddyAdapterTestCase(unittest.TestCase):
    """公共脚手架：隔离 workspace + 临时 HOME + fixture 可执行文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.store = self.root / "store"
        self.fixture = self.root / "codebuddy-fixture.py"
        self.fixture.write_text(FIXTURE_SOURCE, encoding="utf-8")
        os.chmod(self.fixture, 0o700)
        self.task = task_record(self.workspace)
        self.attempt = attempt_record()

    def tearDown(self):
        self.tmp.cleanup()

    def transport(self, mode="ok", timeout=60.0, **over):
        env = {"HOME": str(self.home), "PATH": "/usr/bin:/bin", "WB_FIXTURE_MODE": mode}
        env.update(over.pop("env", {}))
        return CodeBuddyTransport(
            command=[sys.executable, str(self.fixture)],
            workspace=str(self.workspace),
            store_dir=str(self.store),
            env=env,
            timeout=timeout,
            **over,
        )

    def adapter(self, mode="ok", timeout=60.0, **over):
        return WorkBuddyAdapter(self.transport(mode=mode, timeout=timeout, **over), str(self.store))

    def run_count(self):
        path = self.workspace / "run-count.txt"
        return len(path.read_text(encoding="utf-8").splitlines()) if path.is_file() else 0


class WorkBuddyAdapterHappyPathTests(WorkBuddyAdapterTestCase):
    def test_accepts_when_session_id_is_echoed(self):
        adapter = self.adapter()
        result = adapter.submit(self.task, self.attempt)
        self.assertEqual(result.status, "accepted_by_host")
        self.assertEqual(result.source_id, WORKBUDDY_SOURCE_ID)
        self.assertEqual(result.host_receipt, "att-wb-1")
        self.assertEqual(result.evidence_level, "LOCAL")
        receipt = adapter.get_status("att-wb-1")
        self.assertEqual(receipt["hostState"], "completed")
        self.assertTrue(receipt["sessionPersistent"])
        self.assertEqual(receipt["exitCode"], 0)
        self.assertEqual(receipt["toolDeniedCount"], 0)
        # 原始事件与脱敏摘要都要落盘
        self.assertTrue(Path(receipt["eventsPath"]).is_file())
        self.assertTrue(Path(receipt["safeEventsPath"]).is_file())

    def test_collect_returns_real_digests(self):
        adapter = self.adapter()
        adapter.submit(self.task, self.attempt)
        artifacts = adapter.collect(self.task, self.attempt, ["result.md"])
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(artifacts[0]["path"], "result.md")
        self.assertEqual(artifacts[0]["size"], len(b"WB_WRITE_OK"))
        self.assertEqual(len(artifacts[0]["sha256"]), 64)
        self.assertEqual(
            artifacts[0]["sha256"],
            __import__("hashlib").sha256(b"WB_WRITE_OK").hexdigest(),
        )

    def test_capabilities_report_unverified_restart_recovery(self):
        caps = self.adapter().capabilities()
        self.assertTrue(caps.submit)
        self.assertTrue(caps.host_acceptance)
        self.assertFalse(caps.restart_recovery)
        self.assertEqual(caps.evidence_level, "LOCAL")

    def test_prompt_carries_task_facts_and_runs_without_shell(self):
        transport = self.transport()
        argv = transport.argv("wb-att-wb-1", "PROMPT")
        self.assertIn("--output-format", argv)
        self.assertIn("stream-json", argv)
        self.assertIn("acceptEdits", argv)
        self.assertEqual(argv[-1], "PROMPT")
        self.assertNotIn("dontAsk", argv)
        self.assertNotIn("--no-session-persistence", argv)

    def test_ephemeral_mode_is_explicit_only(self):
        transport = self.transport(persist_session=False)
        self.assertIn("--no-session-persistence", transport.argv("wb-att-wb-1", "PROMPT"))


class WorkBuddyIdempotencyTests(WorkBuddyAdapterTestCase):
    def test_repeat_submit_replays_without_second_execution(self):
        adapter = self.adapter()
        first = adapter.submit(self.task, self.attempt)
        count_after_first = self.run_count()
        second = adapter.submit(self.task, self.attempt)
        self.assertEqual(first.status, second.status)
        self.assertEqual(first.host_receipt, second.host_receipt)
        self.assertEqual(self.run_count(), count_after_first)
        self.assertIn("replayed", second.note)

    def test_rework_uses_new_attempt_directory(self):
        adapter = self.adapter()
        adapter.submit(self.task, self.attempt)
        second_attempt = attempt_record(attempt_id="att-wb-2")
        adapter.submit(self.task, second_attempt)
        self.assertTrue((self.store / "attempts" / "att-wb-1" / "receipt.json").is_file())
        self.assertTrue((self.store / "attempts" / "att-wb-2" / "receipt.json").is_file())
        self.assertEqual(adapter.get_status("att-wb-2")["sessionId"], "att-wb-2")


class WorkBuddyFailClosedTests(WorkBuddyAdapterTestCase):
    def assertCode(self, mode, expected_code, timeout=60.0, **over):
        adapter = self.adapter(mode=mode, timeout=timeout, **over)
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(self.task, self.attempt)
        self.assertEqual(ctx.exception.code, expected_code)
        return adapter

    def test_missing_init_event_is_host_unconfirmed(self):
        adapter = self.assertCode("no_init", "host_unconfirmed")
        receipt = adapter.get_status("att-wb-1")
        self.assertEqual(receipt["hostState"], "submitted")
        self.assertNotIn("acceptedAt", receipt)

    def test_session_id_mismatch_is_host_unconfirmed(self):
        adapter = self.assertCode("bad_session", "host_unconfirmed")
        self.assertEqual(adapter.get_status("att-wb-1")["reason"], "SESSION_ID_MISMATCH")

    def test_broken_json_is_submit_unknown(self):
        self.assertCode("bad_json", "submit_unknown")

    def test_nonzero_exit_is_submit_unknown(self):
        adapter = self.assertCode("nonzero", "submit_unknown")
        self.assertEqual(adapter.get_status("att-wb-1")["reason"], "NONZERO_EXIT_CODE")

    def test_result_error_flag_is_submit_unknown(self):
        adapter = self.assertCode("result_error", "submit_unknown")
        self.assertEqual(adapter.get_status("att-wb-1")["reason"], "RESULT_IS_ERROR")

    def test_dropped_connection_without_result_is_submit_unknown(self):
        adapter = self.assertCode("no_result", "submit_unknown")
        self.assertEqual(
            adapter.get_status("att-wb-1")["reason"], "NO_RESULT_EVENT_CONNECTION_DROPPED"
        )

    def test_silent_host_times_out_instead_of_hanging(self):
        adapter = self.assertCode("silent", "submit_unknown", timeout=0.6)
        receipt = adapter.get_status("att-wb-1")
        self.assertTrue(receipt["timedOut"])
        self.assertEqual(receipt["reason"], "WORKBUDDY_EXECUTION_TIMEOUT")

    def test_success_without_tool_activity_is_flagged_not_treated_as_delivery(self):
        """实测宿主可在"什么都没做"时返回 subtype=success，必须显式标记。"""
        adapter = self.adapter(mode="no_tool")
        adapter.submit(self.task, self.attempt)
        receipt = adapter.get_status("att-wb-1")
        self.assertTrue(receipt["noToolActivity"])
        self.assertEqual(receipt["toolUseCount"], 0)
        # 没有真实产物时，收集必须失败——不能靠 success 事件补齐证据。
        with self.assertRaises(ContractError) as ctx:
            adapter.collect(self.task, self.attempt, ["result.md"])
        self.assertEqual(ctx.exception.code, "invalid_task")


class WorkBuddyPathGuardTests(WorkBuddyAdapterTestCase):
    def setUp(self):
        super().setUp()
        self.adapter_instance = self.adapter()
        self.adapter_instance.submit(self.task, self.attempt)

    def assertPathEscape(self, entry, expected="path_escape"):
        with self.assertRaises(ContractError) as ctx:
            self.adapter_instance.collect(self.task, self.attempt, [entry])
        self.assertEqual(ctx.exception.code, expected)

    def test_rejects_parent_traversal(self):
        self.assertPathEscape("../escape.txt")

    def test_rejects_absolute_path(self):
        self.assertPathEscape(str(self.root / "outside.txt"))

    def test_rejects_symlink(self):
        (self.workspace / "link.md").symlink_to(self.root / "outside.txt")
        self.assertPathEscape("link.md")

    def test_rejects_missing_file(self):
        self.assertPathEscape("nope.md", expected="invalid_task")

    def test_rejects_symlinked_directory_escape(self):
        (self.workspace / "sub").symlink_to(self.root)
        self.assertPathEscape("sub/anything.md")


class WorkBuddyBindingTests(WorkBuddyAdapterTestCase):
    def test_wrong_source_id_is_rejected(self):
        adapter = self.adapter()
        bad_task = task_record(self.workspace, source_id="qclaw-gateway")
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(bad_task, self.attempt)
        self.assertEqual(ctx.exception.code, "permission_denied")

    def test_attempt_of_other_task_is_rejected(self):
        adapter = self.adapter()
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(self.task, attempt_record(task_id="task-other"))
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_adapter_source_id_cannot_be_overridden(self):
        with self.assertRaises(ValueError):
            WorkBuddyAdapter(self.transport(), str(self.store), source_id="qclaw-gateway")

    def test_attempt_id_with_separator_is_rejected(self):
        adapter = self.adapter()
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(self.task, attempt_record(attempt_id="../escape"))
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_non_private_store_is_rejected(self):
        self.store.mkdir(mode=0o755, exist_ok=True)
        os.chmod(self.store, 0o755)
        with self.assertRaises(ContractError) as ctx:
            WorkBuddyAdapter(self.transport(), str(self.store))
        self.assertEqual(ctx.exception.code, "permission_denied")


class WorkBuddyTransportGuardTests(WorkBuddyAdapterTestCase):
    def test_command_must_be_absolute(self):
        with self.assertRaises(ValueError):
            CodeBuddyTransport(["codebuddy"], str(self.workspace), str(self.store),
                               env={"HOME": str(self.home)})

    def test_env_home_is_required(self):
        with self.assertRaises(ValueError):
            CodeBuddyTransport([sys.executable, str(self.fixture)], str(self.workspace),
                               str(self.store), env={})

    def test_only_accept_edits_permission_mode_is_allowed(self):
        self.assertEqual(ALLOWED_PERMISSION_MODES, ("acceptEdits",))
        for mode in ("dontAsk", "default", "bypassPermissions", "auto"):
            with self.assertRaises(ValueError):
                CodeBuddyTransport([sys.executable, str(self.fixture)], str(self.workspace),
                                   str(self.store), permission_mode=mode,
                                   env={"HOME": str(self.home)})

    def test_parse_stream_line_rejects_non_json_and_non_object(self):
        with self.assertRaises(WorkBuddyTransportError):
            parse_stream_line("not json")
        with self.assertRaises(WorkBuddyTransportError):
            parse_stream_line("[1, 2, 3]")
        with self.assertRaises(WorkBuddyTransportError):
            parse_stream_line('{"subtype": "init"}')

    def test_safe_event_does_not_leak_model_text(self):
        event = {"type": "result", "subtype": "success", "is_error": False,
                 "result": "SECRET-MODEL-TEXT"}
        safe = safe_event(event)
        self.assertNotIn("result", safe)
        self.assertEqual(safe["resultDigest"], __import__("hashlib").sha256(
            b"SECRET-MODEL-TEXT").hexdigest())
        init = safe_event({"type": "system", "subtype": "init", "session_id": "wb-1",
                           "model": "auto", "permissionMode": "acceptEdits",
                           "tools": ["Read", "Write"]})
        self.assertEqual(init["toolCount"], 2)


class WorkBuddyEntryRouteTests(unittest.TestCase):
    """d4ctl 受控入口的 WorkBuddy 路由：身份、来源与参数一致性。

    只验证路由与身份边界，不启动真实宿主（缺失 binary 的失败路径同样 fail closed）。
    脚手架自包含，不依赖其他测试模块的公共基类。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / "d4.sqlite3"
        self.home = self.root / "home"
        self.home.mkdir()
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.allowed = str(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, argv, source="qclaw-gateway"):
        base = ["--db", str(self.db), "--allowed-root", self.allowed,
                "--agent", "dev-1", "--source", source]
        return d4ctl.run(base + list(argv))

    def create_task(self, source="workbuddy-codebuddy", **over):
        payload = {
            "projectId": "conductor",
            "title": "WorkBuddy 入口路由",
            "agentId": "dev-1",
            "sourceId": source,
            "workspace": str(self.workspace),
            "permissions": {"writeScope": ["."], "allowNetwork": False,
                            "allowBusinessWrite": False, "allowGit": False,
                            "allowPublish": False},
            "goal": "验证入口路由与身份边界",
        }
        payload.update(over)
        path = self.root / "task.json"
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return self.run_cli(["task", "create", "--file", str(path)], source=source)

    def test_unknown_source_identity_is_rejected(self):
        code, result = self.run_cli(["query", "--review-queue"], source="evil-source")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_workbuddy_route_requires_workbuddy_source(self):
        code, result = self.run_cli(
            ["attempt", "submit", "--task", "task-1",
             "--workbuddy-bin", "/nonexistent/codebuddy",
             "--workbuddy-home", str(self.home)],
            source="qclaw-gateway")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_workbuddy_source_requires_workbuddy_route(self):
        created = self.create_task()[1]
        self.assertTrue(created["ok"], created)
        code, result = self.run_cli(
            ["attempt", "submit", "--task", created["data"]["taskId"]],
            source="workbuddy-codebuddy")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_partial_workbuddy_config_is_usage_error(self):
        created = self.create_task()[1]
        task_id = created["data"]["taskId"]
        code, result = self.run_cli(
            ["attempt", "submit", "--task", task_id, "--workbuddy-bin", "/nonexistent/codebuddy"],
            source="workbuddy-codebuddy")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_missing_workbuddy_home_is_usage_error(self):
        created = self.create_task()[1]
        task_id = created["data"]["taskId"]
        code, result = self.run_cli(
            ["attempt", "submit", "--task", task_id,
             "--workbuddy-bin", "/nonexistent/codebuddy",
             "--workbuddy-home", str(self.root / "no-such-home")],
            source="workbuddy-codebuddy")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_workbuddy_submit_with_unlaunchable_host_fails_closed(self):
        """宿主不可启动时必须记 unknown，绝不允许回落成"已接收"。"""
        created = self.create_task()[1]
        task_id = created["data"]["taskId"]
        code, result = self.run_cli(
            ["attempt", "submit", "--task", task_id,
             "--workbuddy-bin", "/nonexistent/codebuddy",
             "--workbuddy-home", str(self.home)],
            source="workbuddy-codebuddy")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "submit_unknown")

    def test_workbuddy_session_id_cannot_be_supplied(self):
        """--workbuddy-session 必须不存在：开放它等于给 accepted_by_host 留伪造口。"""
        created = self.create_task()[1]
        task_id = created["data"]["taskId"]
        with self.assertRaises(SystemExit):
            self.run_cli(
                ["attempt", "submit", "--task", task_id,
                 "--workbuddy-bin", "/nonexistent/codebuddy",
                 "--workbuddy-home", str(self.home),
                 "--workbuddy-session", "forged"],
                source="workbuddy-codebuddy")


class WorkBuddyControlTests(WorkBuddyAdapterTestCase):
    def test_workbuddy_prompt_uses_real_workspace_and_task_authorization(self):
        message = WorkBuddyAdapter._build_prompt(self.task, self.attempt)
        self.assertIn("本轮回执目录：" + str(self.workspace), message)
        self.assertNotIn("唯一写入目录：/workspace/", message)
        self.assertNotIn("不用 exec", message)
        self.assertIn("任务权限为准", message)

    def test_fixed_model_and_expert_argv_and_receipt(self):
        self.task["agent_id"] = "dev-wkb1"
        binding = {"hostAgent": "dev-wkb1", "pluginDir": "/fixture/expert"}
        transport = self.transport(model=DEFAULT_WORKBUDDY_MODEL, binding=binding)
        argv = transport.argv("att-wb-1", "--prompt-like-option")
        self.assertEqual(argv[argv.index("--model") + 1], DEFAULT_WORKBUDDY_MODEL)
        self.assertEqual(argv[argv.index("--agent") + 1], "dev-wkb1")
        self.assertEqual(argv[-2:], ["--", "--prompt-like-option"])
        adapter = WorkBuddyAdapter(transport, str(self.store))
        adapter.submit(self.task, self.attempt)
        receipt = adapter.get_status("att-wb-1")
        self.assertEqual(receipt["observedModel"], DEFAULT_WORKBUDDY_MODEL)
        self.assertEqual(receipt["binding"], binding)
        runtime = json.loads((self.store / "attempts/att-wb-1/runtime.json").read_text())
        self.assertEqual(runtime["state"], "exited")
        self.assertEqual(runtime["workspace"], str(self.workspace))

    def test_model_mismatch_preserves_unknown_and_live_evidence(self):
        adapter = self.adapter(mode="bad_model", model=DEFAULT_WORKBUDDY_MODEL)
        with self.assertRaises(ContractError) as caught:
            adapter.submit(self.task, self.attempt)
        self.assertEqual(caught.exception.code, "submit_unknown")
        with self.assertRaises(ContractError):
            adapter.submit(self.task, self.attempt)
        self.assertEqual(self.run_count(), 1)
        live = self.store / "attempts/att-wb-1/events.live.jsonl"
        self.assertIn("wrong-model", live.read_text())

    def test_no_automatic_model_fallback(self):
        with self.assertRaisesRegex(ValueError, "MODEL_NOT_ALLOWED"):
            self.transport(model="auto")
        argv = self.transport(model=DEFAULT_WORKBUDDY_MODEL).argv("att-wb-1", "test")
        self.assertNotIn("--fallback-model", argv)

    def test_authorized_models_use_exact_id_and_keep_mismatch_unknown(self):
        for model in ("hy4-preview", "glm-5.3-flash", "custom-local:glm-5.3-flash"):
            with self.subTest(model=model):
                argv = self.transport(model=model).argv("att-wb-1", "test")
                self.assertEqual(argv[argv.index("--model") + 1], model)
                self.assertNotIn("--fallback-model", argv)
                self.assertNotIn("--bg", argv)
                adapter = self.adapter(mode="bad_model", model=model)
                attempt = {**self.attempt, "attempt_id": "att-" + model}
                with self.assertRaises(ContractError) as caught:
                    adapter.submit(self.task, attempt)
                self.assertEqual(caught.exception.code, "submit_unknown")

    def test_custom_glm_id_round_trips_without_normalization(self):
        model = "custom-local:glm-5.3-flash"
        adapter = self.adapter(model=model)
        adapter.submit(self.task, self.attempt)
        receipt = adapter.get_status(self.attempt["attempt_id"])
        self.assertEqual(receipt["requestedModel"], model)
        self.assertEqual(receipt["observedModel"], model)
        self.assertNotIn("--fallback-model", self.transport(model=model).argv("att-wb-1", "test"))

    def test_explicit_agent_and_workspace_mismatch_stop(self):
        for mode in ("bad_agent", "bad_cwd"):
            with self.subTest(mode=mode):
                transport = self.transport(mode=mode, model=DEFAULT_WORKBUDDY_MODEL,
                    binding={"hostAgent": "dev-wkb1", "pluginDir": "/fixture"})
                with self.assertRaises(WorkBuddyTransportError):
                    transport.run("att-" + mode, "test")

    def test_frozen_expert_body_and_policy_are_passed(self):
        path = self.root / "expert.md"
        text = "---\nname: dev-wkb1\n---\nFixed worker instructions"
        path.write_text(text)
        binding = {"hostAgent": "dev-wkb1", "pluginDir": "/fixture",
                   "profileMode": "frozen-expert-instructions", "definitionPath": str(path),
                   "definitionSha256": __import__("hashlib").sha256(text.encode()).hexdigest(),
                   "disallowedTools": ["WebFetch", "WebSearch"], "maxTurns": 60}
        transport = self.transport(model=DEFAULT_WORKBUDDY_MODEL, binding=binding)
        result = transport.run("att-fixed", "test")
        frozen = self.store / "attempts/att-fixed/expert-prompt.md"
        self.assertEqual(frozen.read_text().strip(), "Fixed worker instructions")
        self.assertEqual(result["argv"][result["argv"].index("--system-prompt-file") + 1], str(frozen))
        path.write_text("changed")
        with self.assertRaisesRegex(WorkBuddyTransportError, "EXPERT_DRIFT"):
            transport.run("att-drift", "test")

    def test_expert_binding_rejects_unknown_member_and_records_digests(self):
        with self.assertRaisesRegex(ValueError, "MEMBER_NOT_BOUND"):
            workbuddy_binding("qa-3")
        plugin = self.root / "expert"
        (plugin / ".codebuddy-plugin").mkdir(parents=True)
        (plugin / "agents").mkdir()
        manifest = plugin / ".codebuddy-plugin/plugin.json"
        manifest.write_text(json.dumps({"name": "dev-wkb1", "agentName": "dev-wkb1",
                                       "agents": ["./agents/dev-wkb1.md"], "expertType": "agent"}))
        definition = plugin / "agents/dev-wkb1.md"
        definition.write_text("---\nname: dev-wkb1\ndisallowedTools: WebFetch,WebSearch\nmaxTurns: 60\n---\nfixture")
        with mock.patch("adapters.workbuddy.WORKBUDDY_PLUGIN_ROOT", plugin):
            binding = workbuddy_binding("dev-wkb1")
            self.assertEqual(len(binding["definitionSha256"]), 64)
            self.assertEqual(binding["runtimeIdentity"], "UNVERIFIED")
            definition.unlink()
            definition.symlink_to(self.fixture)
            with self.assertRaisesRegex(ValueError, "FILE_INVALID"):
                workbuddy_binding("dev-wkb1")

    def test_disabled_or_duplicate_member_rejected_before_host(self):
        registry = self.root / "registry.md"
        line = "| dev-wkb1 | WorkBuddy（Mac） | model | worker | scope | rights | disabled |\n"
        registry.write_text(line)
        with mock.patch("adapters.workbuddy.WORKBUDDY_REGISTRY", registry):
            with self.assertRaisesRegex(ValueError, "NOT_ACTIVE"):
                workbuddy_binding("dev-wkb1")
            registry.write_text(line.replace("disabled", "active") * 2)
            with self.assertRaisesRegex(ValueError, "AMBIGUOUS"):
                workbuddy_binding("dev-wkb1")

    def test_dev_wkb1_cli_route_has_model_and_binding(self):
        db = self.root / "d4.sqlite"
        payload = self.root / "task.json"
        payload.write_text(json.dumps({"projectId": "d4-controls", "title": "fixture",
            "agentId": "dev-wkb1", "sourceId": WORKBUDDY_SOURCE_ID,
            "workspace": str(self.workspace), "goal": "fixture",
            "permissions": {"writeScope": ["."], "allowNetwork": False,
                            "allowBusinessWrite": False, "allowGit": False, "allowPublish": False}}))
        base = ["--db", str(db), "--allowed-root", str(self.root),
                "--source", WORKBUDDY_SOURCE_ID, "--agent", "dev-wkb1"]
        code, created = d4ctl.run(base + ["task", "create", "--file", str(payload)])
        self.assertEqual(code, 0, created)
        binding = {"hostAgent": "dev-wkb1", "pluginDir": "/fixture"}
        real_class = CodeBuddyTransport
        original_init = real_class.__init__

        def fixture_init(instance, **kwargs):
            self.assertEqual(kwargs["env"]["CODEBUDDY_DISABLE_AUTO_MEMORY"], "1")
            self.assertEqual(kwargs["env"]["CODEBUDDY_MEMORY_ENABLED"], "false")
            self.assertEqual(kwargs["env"]["CODEBUDDY_TEAM_MEMORY_ENABLED"], "false")
            kwargs["command"] = [sys.executable, str(self.fixture)]
            original_init(instance, **kwargs)

        with mock.patch.object(d4ctl, "workbuddy_binding", return_value=binding), \
                mock.patch.object(real_class, "__init__", fixture_init):
            code, result = d4ctl.run(base + ["attempt", "submit", "--task", created["data"]["taskId"],
                "--workbuddy-bin", str(self.fixture), "--workbuddy-home", str(self.home)])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["executionFacts"]["requestedModel"], DEFAULT_WORKBUDDY_MODEL)


if __name__ == "__main__":
    unittest.main()
