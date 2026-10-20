"""D4-DSH-02-SDK-001 定向测试：DSH 0.2 SDK 薄封装适配器（adapters/dsh_sdk.py）。

覆盖卡片 §5.2/§5.3/§7 对 SDK 路径的关键断言（全部离线，真实 SDK 一次没跑过）：

- 版本门禁在**任务 SDK 启动前**（`initialize` 前）拒绝不兼容版本；
  不是支持基线不消耗 attempt、不启动任务 SDK、不调 session/prompt。
- 目录绑定（P1-2 SDK 路径同风险）：transport.workspace 与 task.workspace
  的 realpath 不一致 → invalid_task 零任务启动；同 realpath 别名正例通过。
- 接收事实 = `session/prompt` 原始响应的非空 messageId（initialize 只表示就绪）；
  accepted=True + 空 messageId / 缺 messageId → 只停在 submitted，不伪造接收。
- prompt 成功但执行失败（MISSING_CREDENTIAL）= 已接收但执行失败：
  status 仍 accepted_by_host，不把执行失败写成未接收，也不写成功终态。
- 幂等：同 attempt 同内容（requestDigest）回放首次结果；内容变化
  DSH_ATTEMPT_CONFLICT 拒绝；新适配器实例（模拟新进程）从磁盘回放不重投。
- initialize 失败 / prompt 异常 = submit_unknown，不自动重试。
- 生命周期收尾 wait_and_close：终态→close；runtimeClosed 如实上报，不猜成功。
- 预留接口（wait_for_terminal/list_host_tasks/fetch_history）fail closed，
  resume 明确拒绝。
- 受控常量：source 固定 dsh-sdk、agent 固定 dsh-sdk-exec-001，构造不可覆盖。

Mock 证据全部标 SYNTHETIC（离线夹具，非真实宿主证据）。
"""
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from adapters.dsh_sdk import (
    D4_DSH_SDK_AGENT_ID,
    DSH_SDK_PROFILE,
    DSH_SDK_SOURCE_ID,
    DSH_SDK_ATTEMPT_CONFLICT,
    DSH_SDK_SUBMISSION_UNCERTAIN,
    DSH_SDK_VERSION_UNSUPPORTED,
    DSH_SDK_WORKSPACE_MISMATCH,
    SUPPORTED_SDK_VERSIONS,
    DshSdkAdapter,
    normalize_version,
)
from contracts.schemas import ContractError
from service.context import owner_context, source_context, worker_context
from service.events import EventService
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import runtime_root

#: 离线版本桩：`--version` 输出支持基线，任务启动分支永不执行（零启动断言用）。
VERSION_STUB = '''\
import os, sys
args = sys.argv[1:]
if "--version" in args:
    sys.stdout.write(os.environ.get("DSH_STUB_VERSION", "0.2.0-rc.2") + "\\n")
    sys.exit(int(os.environ.get("DSH_STUB_VERSION_EXIT", "0")))
# 任务 SDK 分支：一旦执行说明门禁失效（用可见痕迹暴露）
sys.stderr.write("TASK_SDK_LAUNCHED\\n")
sys.exit(3)
'''


def _ctx_owner(actor_id="cox"):
    return owner_context(actor_id=actor_id, entry="dsh-sdk-adapter-test", synthetic=True)


def _ctx_worker(actor_id="dev-1"):
    return worker_context(actor_id=actor_id, entry="dsh-sdk-adapter-test", synthetic=True)


def _ctx_source(source_id=DSH_SDK_SOURCE_ID):
    return source_context(source_id=source_id, entry="dsh-sdk-adapter-test", synthetic=True)


class FakeLegacyTransport:
    """离线夹具：形状对齐 `orchestrator.dsh.DSHAdapter`（legacy stdio transport）。

    只实现 DshSdkAdapter 用到的接口（start/submit/close/get_status/TERMINAL/
    workspace/command/env/child）。`command` 指向本文件内置的版本桩脚本：
    `--version` 输出受控版本（默认支持基线），任务启动分支写入可见痕迹后
    退出（用于断言零任务启动）。`start_receipt` 模拟 initialize 握手 health；
    `submit_receipt` 模拟 session/prompt 的原始响应。

    全部证据 SYNTHETIC（离线夹具，非真实宿主证据）。
    """

    TERMINAL = {"failed", "unknown", "awaiting_validation"}

    def __init__(self, workspace, *, version="0.2.0-rc.2", start_error=None,
                 start_health=None, submit_error=None, submit_receipt=None,
                 statuses=None, close_error=None):
        self.workspace = Path(workspace)
        # 版本桩以 `-c` 内联源码注入：不在仓库落地任何清单外文件。
        # `python -c SRC --version` 时 sys.argv[1:] == ["--version"]。
        self.command = [sys.executable, "-c", VERSION_STUB]
        self.env = {"HOME": str(Path(workspace).anchor), "PATH": "/usr/bin:/bin",
                    "DSH_STUB_VERSION": version}
        self.child = None
        self.version = version
        self.start_error = start_error
        self.start_health = start_health if start_health is not None else {
            "processAlive": True, "protocolReady": True, "owner": True}
        self.submit_error = submit_error
        self.submit_receipt = submit_receipt if submit_receipt is not None else {
            "messageId": "msg-1", "accepted": True, "sessionId": "session-1",
            "state": "accepted"}
        self.statuses = list(statuses or [])
        self.close_error = close_error
        self.start_calls = 0
        self.submit_calls = []
        self.close_calls = 0

    def start(self):
        self.start_calls += 1
        if self.start_error is not None:
            raise self.start_error
        return dict(self.start_health)

    def submit(self, task_id, revision, attempt_id, prompt, instruction_digest):
        self.submit_calls.append({"taskId": task_id, "revision": revision,
                                  "attemptId": attempt_id, "prompt": prompt,
                                  "instructionDigest": instruction_digest})
        if self.submit_error is not None:
            raise self.submit_error
        return dict(self.submit_receipt)

    def get_status(self, attempt_id):
        if self.statuses:
            item = self.statuses.pop(0)
            if callable(item):
                return item(attempt_id)
            return dict(item)
        # 默认终态：第一次 wait 未关时 runtimeClosed=False；close 后变 True
        # （对齐 legacy transport close() 后账本的 runtimeClosed 语义）。
        if self.close_calls:
            return {"state": "awaiting_validation", "endReason": {"kind": "completed"},
                    "runtimeClosed": True}
        return {"state": "awaiting_validation", "endReason": {"kind": "completed"},
                "runtimeClosed": False}

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error


class DshSdkTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.out = self.workspace / "out"
        self.out.mkdir()
        self.store_dir = self.root / "sdk-store"
        self.store_dir.mkdir(mode=0o700)
        self.db = self.root / "d4.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    # -- 夹具 -------------------------------------------------------------
    def adapter(self, transport, **over):
        return DshSdkAdapter(transport, self.store_dir, **over)

    def raw_task(self, workspace=None, **over):
        base = {
            "task_id": "task-sdk-1", "project_id": "conductor",
            "title": "DSH SDK 薄封装离线验证", "agent_id": "dev-1",
            "source_id": DSH_SDK_SOURCE_ID, "workspace": str(workspace or self.workspace),
            "goal": "离线验证 DSH SDK 适配器边界。", "acceptance_json": '["messageId 可追溯"]',
            "permissions_json": json.dumps({
                "writeScope": ["out/"], "allowNetwork": False,
                "allowBusinessWrite": False, "allowGit": False, "allowPublish": False,
            }, ensure_ascii=False),
            "limits_json": json.dumps({"maxAttempts": 2, "maxReviewRounds": 2,
                                       "timeoutMinutes": 60}),
            "status": "active", "revision": 1,
            "current_attempt_id": "att-sdk-1", "current_delivery_id": None,
        }
        base.update(over)
        return base

    def raw_attempt(self, task, **over):
        base = {"attempt_id": task["current_attempt_id"], "task_id": task["task_id"],
                "created_at": "2026-10-03T00:00:00+08:00", "terminal_at": None,
                "state": "created"}
        base.update(over)
        return base

    def service_payload(self, **over):
        payload = {
            "projectId": "conductor", "title": "DSH SDK 链路离线验证",
            "agentId": "dev-1", "sourceId": DSH_SDK_SOURCE_ID,
            "workspace": str(self.workspace),
            "permissions": {"writeScope": ["out/"], "allowNetwork": False,
                            "allowBusinessWrite": False, "allowGit": False,
                            "allowPublish": False},
            "goal": "验证 DSH SDK 提交、接收与生命周期边界。",
        }
        payload.update(over)
        return payload


# =====================================================================
# 1. 版本门禁：任务 SDK 启动前拒绝（不消耗 attempt、不 dispatch）
# =====================================================================
class VersionGateTests(DshSdkTestBase):
    def test_supported_baseline_constant(self):
        self.assertEqual(SUPPORTED_SDK_VERSIONS, ("0.2.0-rc.2",))

    def test_version_gate_rejects_before_initialize(self):
        """版本不支持 → 不调 start/prompt、不落 submissions（不消耗 attempt）。"""
        transport = FakeLegacyTransport(self.workspace, version="0.1.1-rc.2")
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(ctx.exception.details["dshCode"], DSH_SDK_VERSION_UNSUPPORTED)
        self.assertEqual(transport.start_calls, 0)
        self.assertEqual(transport.submit_calls, [])
        self.assertEqual(list((self.store_dir / "submissions").glob("*.json")), [])

    def test_version_gate_parses_and_matches_baseline(self):
        transport = FakeLegacyTransport(self.workspace)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        result = self.adapter(transport).submit(task, attempt)
        self.assertEqual(result.status, "accepted_by_host")
        note = json.loads(result.note)
        self.assertEqual(note["version"], "0.2.0-rc.2")

    def test_version_gate_runs_again_on_duplicate_before_replay(self):
        """版本门禁先于幂等回放：已回放的 attempt 在版本漂移后仍拒绝。"""
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        drifted = FakeLegacyTransport(self.workspace, version="0.3.0")
        with self.assertRaises(ContractError) as ctx:
            self.adapter(drifted).submit(task, attempt)
        self.assertEqual(ctx.exception.details["dshCode"], DSH_SDK_VERSION_UNSUPPORTED)
        self.assertEqual(drifted.start_calls, 0)

    def test_normalize_version_strict(self):
        self.assertEqual(normalize_version("0.2.0-rc.2\n"), "0.2.0-rc.2")
        self.assertIsNone(normalize_version(""))
        self.assertIsNone(normalize_version("--version"))


# =====================================================================
# 2. 目录绑定（P1-2 SDK 路径）：realpath 不一致零任务启动
# =====================================================================
class WorkspaceBindingTests(DshSdkTestBase):
    def test_workspace_mismatch_zero_launch(self):
        declared = self.root / "declared"
        actual = self.root / "actual"
        for path in (declared, actual):
            path.mkdir()
        transport = FakeLegacyTransport(actual)
        task = self.raw_task(workspace=declared)
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, self.raw_attempt(task))
        self.assertEqual(ctx.exception.code, "invalid_task")
        self.assertEqual(ctx.exception.details.get("dshCode"), DSH_SDK_WORKSPACE_MISMATCH)
        self.assertEqual(transport.start_calls, 0)
        self.assertEqual(transport.submit_calls, [])
        self.assertEqual(list((self.store_dir / "submissions").glob("*.json")), [])

    def test_workspace_alias_same_realpath_passes(self):
        real = os.path.realpath(str(self.workspace))
        alias = str(self.workspace)
        if real == alias:
            self.skipTest("本机临时目录无别名前缀")
        transport = FakeLegacyTransport(real)
        task = self.raw_task(workspace=alias)
        result = self.adapter(transport).submit(task, self.raw_attempt(task))
        self.assertEqual(result.status, "accepted_by_host")

    def test_workspace_relative_rejected(self):
        transport = FakeLegacyTransport(self.workspace)
        task = self.raw_task(workspace="relative/ws")
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, self.raw_attempt(task))
        self.assertEqual(ctx.exception.code, "invalid_task")

    def test_binding_checked_after_version_before_prompt(self):
        """顺序：版本门禁（过）→ 目录绑定（拒）→ 不启动任务 SDK。"""
        transport = FakeLegacyTransport(self.workspace)  # 版本 OK
        task = self.raw_task(workspace=self.root / "elsewhere")
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, self.raw_attempt(task))
        self.assertEqual(ctx.exception.details.get("dshCode"), DSH_SDK_WORKSPACE_MISMATCH)
        self.assertEqual(transport.start_calls, 0)


# =====================================================================
# 3. 接收事实：messageId 才是证据；initialize 只表示就绪
# =====================================================================
class AcceptanceTests(DshSdkTestBase):
    def test_message_id_is_the_receipt(self):
        transport = FakeLegacyTransport(self.workspace, submit_receipt={
            "messageId": "msg-abc-123", "accepted": True,
            "sessionId": "session-xyz", "state": "accepted"})
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        result = self.adapter(transport).submit(task, attempt)
        self.assertEqual(result.status, "accepted_by_host")
        self.assertEqual(result.host_receipt, "msg-abc-123")
        self.assertEqual(result.evidence_level, "LOCAL")
        note = json.loads(result.note)
        self.assertEqual(note["hostSessionId"], "session-xyz")
        self.assertEqual(note["profile"], DSH_SDK_PROFILE)

    def test_blank_message_id_is_submit_unknown(self):
        """R1-7（cox review F7）：accepted=True + 空 messageId → submit_unknown。

        无宿主接收编号不能标成确定提交结果（旧语义 submitted 已纠正）；
        不自动重试、不落 submissions（不产生可回放的"成功"事实）。
        """
        transport = FakeLegacyTransport(self.workspace, submit_receipt={
            "messageId": "   ", "accepted": True, "sessionId": "session-1"})
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")
        self.assertEqual(ctx.exception.details["phase"], "prompt")
        self.assertEqual(list((self.store_dir / "submissions").glob("*.json")), [])

    def test_missing_message_id_is_submit_unknown(self):
        transport = FakeLegacyTransport(self.workspace, submit_receipt={
            "accepted": True, "sessionId": "session-1"})
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")

    def test_accepted_false_with_message_id_is_submit_unknown(self):
        """accepted 与 messageId 不一致（accepted=False）→ submit_unknown。"""
        transport = FakeLegacyTransport(self.workspace, submit_receipt={
            "messageId": "msg-1", "accepted": False})
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")

    def test_transport_state_unknown_is_submit_unknown(self):
        """legacy transport 返回 state=unknown → submit_unknown，不自动重试。"""
        transport = FakeLegacyTransport(self.workspace, submit_receipt={
            "messageId": "msg-1", "accepted": True, "sessionId": "s-1",
            "state": "unknown"})
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")
        self.assertEqual(ctx.exception.details.get("reason"),
                         "transport-state-unknown")

    def test_execution_failure_after_prompt_keeps_acceptance(self):
        """P1 语义：prompt 成功 → MISSING_CREDENTIAL 仍是已接收但执行失败。

        状态仍 accepted_by_host（宿主已入队消息）；执行失败由事件/采集路径
        推进（failed），适配器不把执行失败写成"未接收"，也不写成功终态。
        """
        transport = FakeLegacyTransport(self.workspace, submit_receipt={
            "messageId": "msg-mc-1", "accepted": True, "sessionId": "session-mc",
            "state": "failed", "endReason": {"kind": "error",
                                             "code": "MISSING_CREDENTIAL"}})
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        result = self.adapter(transport).submit(task, attempt)
        self.assertEqual(result.status, "accepted_by_host")
        self.assertEqual(result.host_receipt, "msg-mc-1")
        note = json.loads(result.note)
        self.assertEqual(note["hostState"], "failed")
        self.assertEqual(note["endReasonKind"], "error")

    def test_initialize_not_ready_fails_closed(self):
        transport = FakeLegacyTransport(
            self.workspace,
            start_health={"processAlive": True, "protocolReady": False})
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(transport.submit_calls, [])

    def test_prompt_exception_is_submit_unknown_no_retry(self):
        transport = FakeLegacyTransport(
            self.workspace, submit_error=RuntimeError("connection reset"))
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")
        self.assertEqual(ctx.exception.details["dshCode"], DSH_SDK_SUBMISSION_UNCERTAIN)
        # 未知结果不落 submissions（不产生可回放的"成功"事实）
        self.assertEqual(list((self.store_dir / "submissions").glob("*.json")), [])

    def test_initialize_exception_is_submit_unknown(self):
        transport = FakeLegacyTransport(self.workspace, start_error=RuntimeError("spawn EACCES"))
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, attempt)
        self.assertEqual(ctx.exception.code, "submit_unknown")
        self.assertEqual(ctx.exception.details["phase"], "initialize")
        self.assertEqual(transport.submit_calls, [])


# =====================================================================
# 4. 幂等：同 attempt 同内容回放；内容变化冲突；新实例从磁盘回放
# =====================================================================
class IdempotencyTests(DshSdkTestBase):
    def test_duplicate_submit_replays_without_relaunch(self):
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        first = adapter.submit(task, attempt)
        second = adapter.submit(task, attempt)
        self.assertEqual(len(transport.submit_calls), 1)
        self.assertEqual(first.status, second.status)
        self.assertEqual(first.host_receipt, second.host_receipt)

    def test_new_instance_replays_from_disk(self):
        """服务重启 → 新适配器实例只读 submissions 回放，不重新启动宿主。"""
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        first = adapter.submit(task, attempt)
        fresh = DshSdkAdapter(transport, self.store_dir)
        replayed = fresh.submit(task, attempt)
        self.assertEqual(len(transport.submit_calls), 1)
        self.assertEqual(replayed.status, first.status)
        self.assertEqual(replayed.host_receipt, first.host_receipt)

    def test_duplicate_with_changed_content_conflicts(self):
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        task = self.raw_task()
        attempt = self.raw_attempt(task)
        adapter.submit(task, attempt)
        changed = self.raw_task(goal="完全不同的目标")
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(changed, self.raw_attempt(changed))
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(ctx.exception.details["dshCode"], DSH_SDK_ATTEMPT_CONFLICT)
        self.assertEqual(len(transport.submit_calls), 1)

    def test_attempt_binding_mismatch_rejected(self):
        transport = FakeLegacyTransport(self.workspace)
        task = self.raw_task()
        with self.assertRaises(ContractError) as ctx:
            self.adapter(transport).submit(task, self.raw_attempt(task, task_id="other"))
        self.assertEqual(ctx.exception.code, "invalid_transition")

    def test_submission_records_are_private(self):
        transport = FakeLegacyTransport(self.workspace)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        self.adapter(transport).submit(task, attempt)
        for path in (self.store_dir / "submissions").glob("*.json"):
            mode = stat.S_IMODE(path.stat().st_mode)
            self.assertEqual(mode & 0o077, 0)
            record = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(record["attemptId"], attempt["attempt_id"])
            self.assertEqual(record["version"], "0.2.0-rc.2")
            self.assertEqual(record["profile"], DSH_SDK_PROFILE)


# =====================================================================
# 5. 生命周期收尾：wait_and_close（R1-5：超时/异常也受控收尾；停稳只认事实）
# =====================================================================
class WaitAndCloseTests(DshSdkTestBase):
    def test_wait_and_close_closes_after_terminal(self):
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        summary = adapter.wait_and_close(attempt["attempt_id"], timeout_seconds=2)
        self.assertEqual(summary["finalState"], "awaiting_validation")
        self.assertTrue(summary["runtimeClosed"])
        self.assertFalse(summary["timedOut"])
        self.assertEqual(transport.close_calls, 1)

    def test_wait_and_close_timeout_still_closes_no_orphan(self):
        """R1-5（cox review F5）：超时也进入受控关闭流程，不留孤儿进程。

        旧语义断言 closeCalls=0（超时不关）已纠正：超时必须尝试 close；
        未停稳（runtimeClosed=False）如实上报，不猜成功。
        """
        transport = FakeLegacyTransport(
            self.workspace,
            statuses=[{"state": "running", "runtimeClosed": False}] * 100)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        summary = adapter.wait_and_close(attempt["attempt_id"], timeout_seconds=0.3)
        self.assertEqual(summary["finalState"], "running")
        self.assertTrue(summary["timedOut"])
        # 超时后仍然尝试受控关闭（不留孤儿）
        self.assertGreaterEqual(transport.close_calls, 1)
        # 夹具在 close 后仍回报 running/runtimeClosed=False → 如实上报"未证明停稳"
        self.assertFalse(summary["runtimeClosed"])

    def test_wait_and_close_already_closed_is_idempotent(self):
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        first = adapter.wait_and_close(attempt["attempt_id"], timeout_seconds=2)
        second = adapter.wait_and_close(attempt["attempt_id"], timeout_seconds=2)
        self.assertTrue(first["runtimeClosed"])
        self.assertTrue(second["runtimeClosed"])
        # 第二次调用：状态已是 runtimeClosed=True（终态+停稳），不再需要 close
        self.assertEqual(transport.close_calls, 1)

    def test_wait_and_close_unquiescent_not_reported_stopped(self):
        """R1-5 核心：transport 未停稳（runtimeClosed=False）不能强行改 True。

        模拟 legacy SDK_READER_NOT_QUIESCENT：close 后账本仍 runtimeClosed=False。
        """
        transport = FakeLegacyTransport(
            self.workspace,
            statuses=[{"state": "awaiting_validation",
                       "endReason": {"kind": "completed"}, "runtimeClosed": False}] * 100)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        summary = adapter.wait_and_close(attempt["attempt_id"], timeout_seconds=2)
        self.assertFalse(summary["runtimeClosed"])

    def test_wait_and_close_status_exception_enters_controlled_close(self):
        """get_status 异常（查询异常）也要进入受控收尾，不抛裸异常。"""
        transport = FakeLegacyTransport(
            self.workspace,
            statuses=[RuntimeError("db corrupted")] * 100)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        summary = adapter.wait_and_close(attempt["attempt_id"], timeout_seconds=2)
        self.assertEqual(summary["finalState"], "unknown")
        self.assertGreaterEqual(transport.close_calls, 1)

    def test_wait_and_close_close_failure_reported_not_quiesced(self):
        transport = FakeLegacyTransport(
            self.workspace,
            statuses=[{"state": "awaiting_validation",
                       "endReason": {"kind": "completed"}, "runtimeClosed": False}] * 100,
            close_error=RuntimeError("shutdown refused"))
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        summary = adapter.wait_and_close(attempt["attempt_id"], timeout_seconds=2)
        self.assertFalse(summary["runtimeClosed"])
        self.assertEqual(summary.get("closeError"), "RuntimeError")

    def test_submit_does_not_close_transport(self):
        """收到回执不能立即退出：submit 不关 SDK（事件可能仍在产出）。"""
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        self.assertEqual(transport.close_calls, 0)


# =====================================================================
# 5b. R2-2：close_runtime —— 失败路径收尾（不等待终态、幂等、如实上报）
# =====================================================================
class CloseRuntimeTests(DshSdkTestBase):
    def test_close_runtime_closes_without_waiting_terminal(self):
        """不等待终态：宿主仍在 running 也立即受控关闭。"""
        transport = FakeLegacyTransport(
            self.workspace,
            statuses=[{"state": "running", "runtimeClosed": False}] * 10)
        adapter = self.adapter(transport)
        summary = adapter.close_runtime("att-sdk-1")
        self.assertEqual(summary["finalState"], "running")
        self.assertFalse(summary["timedOut"])
        self.assertEqual(transport.close_calls, 1)

    def test_close_runtime_idempotent(self):
        """幂等：账本已报停稳则不重复 close，重复调用只做只读复核。"""
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        first = adapter.close_runtime("att-sdk-1")
        second = adapter.close_runtime("att-sdk-1")
        self.assertTrue(first["runtimeClosed"])
        self.assertTrue(second["runtimeClosed"])
        self.assertEqual(transport.close_calls, 1)

    def test_close_runtime_close_failure_reported_not_quiesced(self):
        """close 异常：如实上报 closeError，不声称停稳。"""
        transport = FakeLegacyTransport(
            self.workspace,
            statuses=[{"state": "running", "runtimeClosed": False}] * 10,
            close_error=RuntimeError("shutdown refused"))
        adapter = self.adapter(transport)
        summary = adapter.close_runtime("att-sdk-1")
        self.assertEqual(summary.get("closeError"), "RuntimeError")
        self.assertFalse(summary["runtimeClosed"])

    def test_close_runtime_status_exception_still_closes(self):
        """读不到状态也要进入受控关闭，不抛裸异常。"""
        transport = FakeLegacyTransport(
            self.workspace, statuses=[RuntimeError("db corrupted")] * 10)
        adapter = self.adapter(transport)
        summary = adapter.close_runtime("att-sdk-1")
        self.assertEqual(summary["finalState"], "unknown")
        self.assertGreaterEqual(transport.close_calls, 1)

    def test_close_runtime_does_not_submit(self):
        """收尾只关闭，不重新投递（R2-2：重复调用不得再次提交任务）。"""
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        adapter.close_runtime("att-sdk-1")
        adapter.close_runtime("att-sdk-1")
        self.assertEqual(transport.start_calls, 0)
        self.assertEqual(len(transport.submit_calls), 0)


# =====================================================================
# 6. 预留接口 fail closed；受控常量；能力自报
# =====================================================================
class ReservedAndConstantTests(DshSdkTestBase):
    def test_reserved_interfaces_fail_closed(self):
        adapter = self.adapter(FakeLegacyTransport(self.workspace))
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        for call in (lambda: adapter.wait_for_terminal(task, attempt, 1000),
                     lambda: adapter.list_host_tasks(),
                     lambda: adapter.fetch_history(task, attempt)):
            with self.assertRaises(ContractError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "invalid_transition")
            self.assertTrue(ctx.exception.details.get("reserved"))

    def test_source_id_is_fixed(self):
        with self.assertRaises(ValueError):
            DshSdkAdapter(FakeLegacyTransport(self.workspace), self.store_dir,
                          source_id="qclaw-gateway")

    def test_agent_id_is_controlled_constant(self):
        adapter = self.adapter(FakeLegacyTransport(self.workspace))
        self.assertEqual(adapter.agent_id, D4_DSH_SDK_AGENT_ID)
        self.assertEqual(D4_DSH_SDK_AGENT_ID, "dsh-sdk-exec-001")

    def test_transport_shape_validated(self):
        class NoStart:
            workspace = self.workspace
            command = ["/usr/bin/true"]

            def submit(self, *a, **k):
                return {}

            def close(self):
                pass

        with self.assertRaises(ValueError):
            DshSdkAdapter(NoStart(), self.store_dir)

    def test_capability_report_honest(self):
        adapter = self.adapter(FakeLegacyTransport(self.workspace))
        report = adapter.capability_report()
        self.assertEqual(report["adapter"], "dsh-sdk")
        caps = report["capabilities"]
        self.assertTrue(caps["host_acceptance"])
        self.assertTrue(caps["submit"])
        self.assertFalse(caps["restart_recovery"])
        self.assertEqual(caps["evidence_level"], "LOCAL")
        self.assertTrue(report["connected"])

    def test_security_sink_receives_sanitized_events(self):
        events = []
        adapter = DshSdkAdapter(
            FakeLegacyTransport(self.workspace), self.store_dir,
            security_sink=lambda kind, detail: events.append((kind, detail)))
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        kinds = [kind for kind, _ in events]
        self.assertIn("dshsdk.accepted_by_host", kinds)
        # 安全事件只含编号/版本，不含 prompt 原文（脱敏）
        for kind, detail in events:
            self.assertNotIn("prompt", detail)


# =====================================================================
# 7. D4 服务集成：dsh-sdk 来源全链路（离线、SYNTHETIC）
# =====================================================================
class DshSdkServiceIntegrationTests(DshSdkTestBase):
    def _services(self, transport):
        store = SqliteStore(self.db)
        store.register_source(DSH_SDK_SOURCE_ID, label="dsh-sdk", at="t0")
        store.register_agent("dev-1", "D4 DSH SDK worker", "registry", at="t0")
        adapter = DshSdkAdapter(transport, self.store_dir)
        tasks = TaskService(store, adapter=adapter,
                            allowed_roots=[os.path.realpath(str(runtime_root()))])
        events = EventService(store)
        return store, tasks, events, adapter

    def _event(self, task, attempt, seq, kind, payload=None):
        return {
            "eventId": "evt-%s-%d" % (attempt["attemptId"], seq),
            "projectId": task["projectId"], "taskId": task["taskId"],
            "attemptId": attempt["attemptId"], "sourceId": DSH_SDK_SOURCE_ID,
            "agentId": "dev-1", "sequence": seq, "type": kind,
            "occurredAt": "2026-10-03T10:00:00+08:00", "payload": payload or {},
        }

    def test_full_offline_chain_messageid_receipt(self):
        """建卡 → 提交（messageId 接收）→ 交付 → awaiting_review 全链路。"""
        transport = FakeLegacyTransport(self.workspace, submit_receipt={
            "messageId": "msg-chain-1", "accepted": True,
            "sessionId": "session-chain", "state": "accepted"})
        store, tasks, events, adapter = self._services(transport)
        task = tasks.create_task(self.service_payload(), context=_ctx_owner())
        attempt = tasks.create_attempt(task["taskId"], context=_ctx_worker())
        submitted = tasks.submit_attempt(task["taskId"], attempt["attemptId"],
                                         context=_ctx_worker())
        submission = submitted["submission"]
        self.assertEqual(submission["status"], "accepted_by_host")
        self.assertEqual(submission["host_receipt"], "msg-chain-1")
        row = store.get_attempt(attempt["attemptId"])
        self.assertIsNotNone(row["accepted_by_host_at"])
        # 交付 → EventService 写 terminal_at → awaiting_review
        (self.out / "result.md").write_text("r", encoding="utf-8")
        payload = adapter.__class__ and {
            "summary": "SDK 链路离线验证",
            "reportPath": str(self.out / "result.md"),
            "changedFiles": ["out/result.md"],
            "verification": ["定向测试已跑（SYNTHETIC）"], "risks": [], "unfinished": []}
        delivered = events.ingest(self._event(task, attempt, 1, "task.delivered", payload),
                                  context=_ctx_source())
        self.assertTrue(delivered["ok"])
        fresh = store.get_attempt(attempt["attemptId"])
        self.assertEqual(fresh["terminal_kind"], "delivered")
        self.assertEqual(store.get_task(task["taskId"])["status"], "awaiting_review")
        store.close()

    def test_service_rejects_wrong_source_binding(self):
        """适配器 source 固定 dsh-sdk：建 qclaw 任务后提交由服务层拒绝。"""
        store = SqliteStore(self.db)
        store.register_source("qclaw-gateway", label="qclaw", at="t0")
        store.register_agent("dev-1", "worker", "registry", at="t0")
        adapter = DshSdkAdapter(FakeLegacyTransport(self.workspace), self.store_dir)
        tasks = TaskService(store, adapter=adapter,
                            allowed_roots=[os.path.realpath(str(runtime_root()))])
        payload = self.service_payload(sourceId="qclaw-gateway")
        task = tasks.create_task(payload, context=_ctx_owner())
        attempt = tasks.create_attempt(task["taskId"], context=_ctx_worker())
        with self.assertRaises(ContractError) as ctx:
            tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=_ctx_worker())
        self.assertEqual(ctx.exception.code, "permission_denied")
        store.close()

    def test_prompt_contract_carries_d4_identifiers(self):
        """投递消息复用 build_task_message：taskId/attemptId/workspace 必须在 prompt。"""
        transport = FakeLegacyTransport(self.workspace)
        adapter = self.adapter(transport)
        task, attempt = self.raw_task(), self.raw_attempt(self.raw_task())
        adapter.submit(task, attempt)
        prompt = transport.submit_calls[0]["prompt"]
        self.assertIn("taskId: %s" % task["task_id"], prompt)
        self.assertIn("attemptId: %s" % attempt["attempt_id"], prompt)
        self.assertIn("workspace: %s" % task["workspace"], prompt)
        self.assertIn("0.2.0-rc.2", prompt)
        self.assertIn("dsh-sdk", prompt)


if __name__ == "__main__":
    unittest.main()
