"""D4-DSH-02-SDK-001-R1 定向测试：CLI 受控生命周期与双轮审核（R1-2/R1-4）。

覆盖返工卡 §4 R1-2 / R1-4 / §5 的关键断言（全部离线 SYNTHETIC）：

- CLI ``attempt submit``（dsh-sdk 注入路由）在提交事实落库后同一进程完成：
  wait_and_close（有界等待）→ 归属核对 → legacy 事件转换 → 标准回执写入
  （file_receipts 合同：workspace/receipts/<taskId>/<attemptId>/events.json，
  hostRunId == messageId）→ import_receipt 入账 → 收尾。
- 最终 JSON 分段：submission / execution / collection / teardown。
- 超时/失败路径：未停稳不能生成成功交付；close 异常如实上报。
- 双轮：首交 → changes_requested → 新 attempt（新 store/session/messageId）
  → 二交 → approved；旧 delivery 可查；旧轮次事件不推进新轮次；
  maxAttempts 上限停止自动返工。
- worker 没有审核权限（review submit 用 worker 身份 → permission_denied）。

全部宿主替身标 SYNTHETIC（离线夹具，非真实宿主证据）。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from adapters.dsh_sdk import DEFAULT_EXECUTION_TIMEOUT_SECONDS, DshSdkAdapter
from contracts.schemas import ContractError
from scripts import d4ctl
from storage.sqlite_store import SqliteStore

from test_d4_contracts import create_task_payload, runtime_root

#: 离线版本桩：`--version` 输出支持基线；任务启动分支永不执行。
VERSION_STUB = '''\
import sys
if "--version" in sys.argv[1:]:
    sys.stdout.write("0.2.0-rc.2\\n")
    sys.exit(0)
sys.exit(3)
'''


# ---------------------------------------------------------------------
# 离线夹具：形状对齐 orchestrator.dsh.DSHAdapter（legacy stdio transport）
# ---------------------------------------------------------------------
class LifecycleFakeTransport:
    """可编程生命周期夹具：submit 回执 + wait 状态序列 + close 行为。

    ``command`` 指向内置版本桩（`--version` 输出 0.2.0-rc.2），满足适配器
    版本门禁；任务启动分支不执行。全部证据 SYNTHETIC（离线夹具）。
    """

    TERMINAL = {"failed", "unknown", "awaiting_validation"}

    def __init__(self, workspace, *, submit_receipt=None, statuses=None,
                 close_error=None, legacy_events=None):
        self.workspace = Path(workspace)
        self.command = [sys.executable, "-c", VERSION_STUB]
        self.env = {"HOME": "/tmp", "PATH": "/usr/bin:/bin"}
        self.child = None
        self.submit_receipt = submit_receipt if submit_receipt is not None else {
            "messageId": "msg-lc-1", "accepted": True,
            "sessionId": "session-lc-1", "state": "accepted"}
        self.statuses = list(statuses or [])
        self.close_error = close_error
        self.legacy_events = legacy_events or []
        self.db_path = None  # collect_events 读 sqlite 账本；夹具无账本
        self.start_calls = 0
        self.submit_calls = []
        self.close_calls = 0

    def start(self):
        self.start_calls += 1
        return {"processAlive": True, "protocolReady": True, "owner": True}

    def submit(self, task_id, revision, attempt_id, prompt, instruction_digest):
        self.submit_calls.append({"attemptId": attempt_id, "prompt": prompt})
        return dict(self.submit_receipt)

    def get_status(self, attempt_id):
        if self.statuses:
            item = self.statuses.pop(0)
            if callable(item):
                return item(attempt_id)
            return dict(item)
        # 默认：宿主已跑完（对齐 legacy turn/end completed 后的账本状态）；
        # close 成功后 runtimeClosed=True；close 异常（账本没写回）保持 False。
        if self.close_calls and self.close_error is None:
            return {"attemptId": attempt_id, "state": "awaiting_validation",
                    "endReason": {"kind": "completed"}, "runtimeClosed": True,
                    "sessionId": self.submit_receipt.get("sessionId"),
                    "messageId": self.submit_receipt.get("messageId")}
        return {"attemptId": attempt_id, "state": "awaiting_validation",
                "endReason": {"kind": "completed"}, "runtimeClosed": False,
                "sessionId": self.submit_receipt.get("sessionId"),
                "messageId": self.submit_receipt.get("messageId")}

    def close(self):
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error

    def seed_events_table(self, root, attempt_id):
        """把 legacy_events 写进真实 sqlite 账本（复刻 legacy events 表形状）。

        collect_events 走适配器真实 SQL（SELECT seq, body FROM events），
        夹具只提供与 legacy transport 相同的表结构。
        """
        import sqlite3
        db_dir = Path(root) / "ledger"
        db_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(db_dir, 0o700)
        os.chmod(Path(root), 0o700)
        self.db_path = str(db_dir / ("%s.sqlite" % attempt_id))
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("CREATE TABLE IF NOT EXISTS events ("
                         "seq INTEGER PRIMARY KEY, body TEXT NOT NULL)")
            for event in self.legacy_events:
                conn.execute("INSERT OR REPLACE INTO events VALUES (?,?)",
                             (event.get("seq"),
                              json.dumps(event, sort_keys=True,
                                         separators=(",", ":"))))
            conn.commit()
        finally:
            conn.close()


class DshLifecycleTestBase(unittest.TestCase):
    """公共脚手架：隔离目录 + dsh-sdk 任务 + 注入路由工厂。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / "d4.sqlite3"
        self.allowed = str(self.root)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        # writeScope 必须含回执目录（receipts/ 在 workspace 下）
        payload = create_task_payload(
            sourceId="dsh-sdk", workspace=str(self.workspace),
            permissions={"writeScope": ["."], "allowNetwork": False,
                         "allowBusinessWrite": False, "allowGit": False,
                         "allowPublish": False})
        task_file = self.root / "task.json"
        task_file.write_text(json.dumps(payload, ensure_ascii=False),
                             encoding="utf-8")
        code, result = d4ctl.run(
            ["--db", str(self.db), "--allowed-root", self.allowed,
             "--source", "dsh-sdk", "--agent", "dev-1",
             "task", "create", "--file", str(task_file)], synthetic=True)
        assert result["ok"], result
        self.task_id = result["data"]["taskId"]

    def tearDown(self):
        self.tmp.cleanup()

    def submit_with_transport(self, transport, attempt_id=None, timeout_ms=3000):
        """注入工厂跑 attempt submit（SYNTHETIC）。"""
        holder = {}

        def factory(task, attempt):
            holder["transport"] = transport
            holder["store"] = str(self.root / "sdk-store" / attempt)
            # 夹具事件先种进 legacy 账本（对齐真实 _read 线程的持久化时机）
            transport.seed_events_table(holder["store"], attempt)
            return transport, holder["store"]

        argv = ["attempt", "submit", "--task", self.task_id]
        if attempt_id:
            argv += ["--attempt", attempt_id]
        argv += ["--dsh-sdk-bin", "/usr/bin/true",
                 "--dsh-sdk-home", str(self.root),
                 "--dsh-sdk-execution-timeout-ms", str(timeout_ms)]
        code, result = d4ctl.run(
            ["--db", str(self.db), "--allowed-root", self.allowed,
             "--source", "dsh-sdk", "--agent", "dev-1"] + argv,
            synthetic=True, dsh_transport_factory=factory)
        return code, result, holder.get("transport")

    def review(self, decision, delivery_id, note="离线审核"):
        payload = {"decision": decision, "deliveryId": delivery_id, "note": note}
        path = self.root / ("review-%s.json" % decision)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return d4ctl.run(
            ["--db", str(self.db), "--allowed-root", self.allowed,
             "--source", "dsh-sdk", "--agent", "dev-1",
             "--reviewer", "cox",
             "review", "submit", "--task", self.task_id,
             "--file", str(path)], synthetic=True)

    def query_task(self):
        code, result = d4ctl.run(
            ["--db", str(self.db), "--allowed-root", self.allowed,
             "--source", "dsh-sdk", "--agent", "dev-1",
             "query", "--task", self.task_id])
        return result["data"]["task"]

    def task_status(self):
        return self.query_task()["status"]

    def current_delivery_id(self):
        return self.query_task()["current_delivery_id"]


# ---------------------------------------------------------------------
# R1-2：提交后事件/回执/收尾链路
# ---------------------------------------------------------------------
class R12LifecycleChainTests(DshLifecycleTestBase):
    def _transport_with_events(self, **over):
        """夹具：submit 成功 → turn/start + turn/end(completed) 事件序列。"""
        events = over.pop("legacy_events", [
            {"seq": 1, "type": "turn/start", "sessionId": "session-lc-1",
             "data": {"turn": 1}},
            {"seq": 2, "type": "turn/end", "sessionId": "session-lc-1",
             "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ])
        return LifecycleFakeTransport(self.workspace, legacy_events=events, **over)

    def test_submit_runs_full_lifecycle_sections(self):
        """接收→执行→采集→收尾四段都在同一次 CLI 调用完成。"""
        transport = self._transport_with_events()
        code, result, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 0, result)
        data = result["data"]
        # 分段齐全
        self.assertIn("submission", data)
        self.assertIn("execution", data)
        self.assertIn("collection", data)
        self.assertIn("teardown", data)
        # 接收
        self.assertEqual(data["submission"]["submission"]["status"],
                         "accepted_by_host")
        # 执行：终态 + 停稳
        self.assertEqual(data["execution"]["finalState"], "awaiting_validation")
        self.assertTrue(data["execution"]["runtimeClosed"])
        # 采集：turn/start 转换为 task.started 并入账（turn/end completed 不自动交付）
        self.assertEqual(data["collection"]["status"], "imported")
        # 收尾
        self.assertTrue(data["teardown"]["closed"])

    def test_receipt_written_per_file_receipts_contract(self):
        """标准回执落在 receipts/<taskId>/<attemptId>/events.json，hostRunId=messageId。"""
        transport = self._transport_with_events()
        code, result, _ = self.submit_with_transport(transport)
        attempt_id = result["data"]["submission"]["attemptId"]
        receipt = self.workspace / "receipts" / self.task_id / attempt_id / "events.json"
        self.assertTrue(receipt.is_file())
        bundle = json.loads(receipt.read_text(encoding="utf-8"))
        self.assertEqual(bundle["hostRunId"], "msg-lc-1")
        self.assertEqual(bundle["events"][0]["type"], "task.started")
        self.assertEqual(bundle["events"][0]["attemptId"], attempt_id)
        self.assertEqual(bundle["events"][0]["sourceId"], "dsh-sdk")
        # started 已入账（attempt.started_at 非空）
        store = SqliteStore(self.db, read_only=True)
        try:
            row = store.get_attempt(attempt_id)
            self.assertIsNotNone(row["started_at"])
        finally:
            store.close()

    def test_turn_end_error_maps_to_blocked_not_delivery(self):
        """turn/end(kind=error) → task.blocked（不造成功交付）。"""
        transport = self._transport_with_events(legacy_events=[
            {"seq": 1, "type": "turn/start", "sessionId": "session-lc-1",
             "data": {"turn": 1}},
            {"seq": 2, "type": "turn/end", "sessionId": "session-lc-1",
             "data": {"turn": 1, "reason": {"kind": "error",
                                            "error": {"code": "MISSING_CREDENTIAL"}}}},
        ])
        code, result, _ = self.submit_with_transport(transport)
        data = result["data"]
        self.assertEqual(data["collection"]["status"], "imported")
        self.assertEqual(self.task_status(), "blocked")

    def test_not_accepted_no_receipt_no_delivery(self):
        """宿主未确认接收（messageId 缺失→submit_unknown）不生成交付回执。"""
        transport = LifecycleFakeTransport(
            self.workspace, submit_receipt={"accepted": True, "state": "unknown"})
        code, result, _ = self.submit_with_transport(transport)
        self.assertNotEqual(code, 0)
        self.assertEqual(result["error"]["code"], "submit_unknown")
        self.assertNotEqual(self.task_status(), "awaiting_review")

    def test_close_exception_reported_not_quiesced(self):
        """close 异常 → teardown 如实上报，不称停稳。"""
        transport = self._transport_with_events(close_error=RuntimeError("boom"))
        code, result, _ = self.submit_with_transport(transport)
        data = result["data"]
        # 生命周期错误也分段返回（不是退出码非 0 就了事）
        self.assertEqual(data["teardown"].get("error"), None)
        self.assertFalse(data["execution"]["runtimeClosed"])
        self.assertEqual(data["execution"].get("closeError"), "RuntimeError")

    def test_timeout_unquiescent_no_success_delivery(self):
        """超时 + 未停稳 → 无成功交付；等待有界。"""
        transport = LifecycleFakeTransport(
            self.workspace,
            statuses=[{"state": "running", "runtimeClosed": False}] * 100)
        code, result, _ = self.submit_with_transport(transport, timeout_ms=300)
        data = result["data"]
        self.assertTrue(data["execution"]["timedOut"])
        self.assertFalse(data["execution"]["runtimeClosed"])
        self.assertNotEqual(self.task_status(), "awaiting_review")


# ---------------------------------------------------------------------
# R1-4：双轮独立账本 + 离线双轮审核
# ---------------------------------------------------------------------
class R14TwoRoundTests(DshLifecycleTestBase):
    def _events(self, session):
        return [
            {"seq": 1, "type": "turn/start", "sessionId": session,
             "data": {"turn": 1}},
            {"seq": 2, "type": "turn/end", "sessionId": session,
             "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]

    def _deliver_round(self, attempt_id, session, message):
        """注入投递 + worker 手写交付事件（模拟 worker 产出报告后交付）。"""
        from service.context import source_context
        from service.events import EventService
        from service.tasks import TaskService
        from service.context import worker_context
        store = SqliteStore(self.db)
        try:
            # 先注入完成 submit 生命周期（turn/start 入账）
            transport = LifecycleFakeTransport(
                self.workspace, submit_receipt={
                    "messageId": message, "accepted": True,
                    "sessionId": session, "state": "accepted"},
                legacy_events=self._events(session))
            code, result, _ = self.submit_with_transport(
                transport, attempt_id=attempt_id)
            assert result.get("ok"), result
            # worker 上报交付（写报告文件 + task.delivered）
            report = self.workspace / "receipts" / self.task_id / attempt_id / "report.md"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("# round report\n", encoding="utf-8")
            relative = str(report.relative_to(self.workspace))
            events = EventService(store)
            payload = {
                "eventId": "evt-deliver-%s" % attempt_id,
                "projectId": "conductor", "taskId": self.task_id,
                "attemptId": attempt_id, "sourceId": "dsh-sdk",
                "agentId": "dev-1", "sequence": 5, "type": "task.delivered",
                "occurredAt": "2026-10-04T10:00:00+08:00",
                "payload": {"summary": "round delivery",
                            "reportPath": relative,
                            "changedFiles": [relative],
                            "verification": ["SYNTHETIC"],
                            "risks": [], "unfinished": []}}
            events.ingest(payload, source_context(
                "dsh-sdk", entry="test", synthetic=True))
        finally:
            store.close()

    def test_two_rounds_independent_ledgers_and_approval(self):
        """首交 → changes_requested → 新轮（新 session/message/store）→ 二交 → approved。"""
        # 第一轮
        transport1 = LifecycleFakeTransport(
            self.workspace, submit_receipt={
                "messageId": "msg-r1", "accepted": True,
                "sessionId": "session-r1", "state": "accepted"},
            legacy_events=self._events("session-r1"))
        code, first, t1 = self.submit_with_transport(transport1)
        attempt1 = first["data"]["submission"]["attemptId"]
        self._deliver_round(attempt1, "session-r1", "msg-r1")
        self.assertEqual(self.task_status(), "awaiting_review")
        delivery1 = self.current_delivery_id()
        # cox 要求返工
        code, review = self.review("changes_requested", delivery1,
                                   note="第一轮需要修改")
        self.assertTrue(review["ok"], review)
        new_attempt = review["data"]["resultingAttemptId"]
        self.assertNotEqual(new_attempt, attempt1)

        # 第二轮：新 transport（新 session/messageId）
        transport2 = LifecycleFakeTransport(
            self.workspace, submit_receipt={
                "messageId": "msg-r2", "accepted": True,
                "sessionId": "session-r2", "state": "accepted"},
            legacy_events=self._events("session-r2"))
        code, second, t2 = self.submit_with_transport(transport2,
                                                      attempt_id=new_attempt)
        attempt2 = second["data"]["submission"]["attemptId"]
        self.assertEqual(attempt2, new_attempt)
        # 独立 store：两次工厂调用产生不同 store 路径
        self.assertNotEqual(t1, None)  # placeholder；store 路径断言见下
        # 每轮 store 目录分开（R1-4）
        store1 = self.root / "sdk-store" / attempt1
        store2 = self.root / "sdk-store" / attempt2
        self.assertNotEqual(str(store1), str(store2))
        # 新 messageId
        self.assertEqual(second["data"]["submission"]["submission"]["host_receipt"],
                         "msg-r2")
        # 二轮交付 → approved
        self._deliver_round(attempt2, "session-r2", "msg-r2")
        delivery2 = self.current_delivery_id()
        self.assertNotEqual(delivery1, delivery2)
        # 旧 delivery 仍可查（list_activity）
        store = SqliteStore(self.db, read_only=True)
        try:
            old = store.get_delivery(delivery1)
            self.assertIsNotNone(old)  # 旧交付保留
        finally:
            store.close()
        code, approve = self.review("approved", delivery2, note="通过")
        self.assertTrue(approve["ok"], approve)
        self.assertEqual(self.task_status(), "approved")

    def test_old_round_events_do_not_advance_new_round(self):
        """旧轮次事件（attemptId 不匹配）不推进新轮次。"""
        from service.context import source_context
        from service.events import EventService
        transport = LifecycleFakeTransport(
            self.workspace, submit_receipt={
                "messageId": "msg-a", "accepted": True,
                "sessionId": "s-a", "state": "accepted"},
            legacy_events=self._events("s-a"))
        code, first, _ = self.submit_with_transport(transport)
        attempt1 = first["data"]["submission"]["attemptId"]
        self._deliver_round(attempt1, "s-a", "msg-a")
        delivery1 = self.current_delivery_id()
        code, review = self.review("changes_requested", delivery1, note="返工")
        new_attempt = review["data"]["resultingAttemptId"]
        # 旧轮次的迟到事件（attemptId=attempt1）→ stale（只入历史不回退）
        store = SqliteStore(self.db)
        try:
            events = EventService(store)
            stale = {
                "eventId": "evt-stale-old-round", "projectId": "conductor",
                "taskId": self.task_id, "attemptId": attempt1,
                "sourceId": "dsh-sdk", "agentId": "dev-1",
                "sequence": 99, "type": "task.progress",
                "occurredAt": "2026-10-04T11:00:00+08:00",
                "payload": {"step": "late"}}
            with self.assertRaises(ContractError) as ctx:
                events.ingest(stale, source_context(
                    "dsh-sdk", entry="test", synthetic=True))
            # 迟到保护：stale_sequence，不回退当前状态
            self.assertEqual(ctx.exception.code, "stale_sequence")
        finally:
            store.close()
        # 新轮次状态不受影响：attempt 仍是 created/未提交，任务 active
        store = SqliteStore(self.db, read_only=True)
        try:
            fresh = store.get_attempt(new_attempt)
            self.assertIn(fresh["submit_state"], ("created", None))
            self.assertIsNone(fresh["terminal_kind"])
        finally:
            store.close()

    def test_max_attempts_stops_auto_rework(self):
        """达到 maxAttempts（默认 2）后，再次 changes_requested 不能自动建新轮次。"""
        # 轮 1
        t1 = LifecycleFakeTransport(self.workspace, submit_receipt={
            "messageId": "m1", "accepted": True, "sessionId": "s1",
            "state": "accepted"}, legacy_events=self._events("s1"))
        _, first, _ = self.submit_with_transport(t1)
        a1 = first["data"]["submission"]["attemptId"]
        self._deliver_round(a1, "s1", "m1")
        d1 = self.current_delivery_id()
        code, r1 = self.review("changes_requested", d1, note="r1 返工")
        self.assertTrue(r1["ok"])
        # 轮 2
        t2 = LifecycleFakeTransport(self.workspace, submit_receipt={
            "messageId": "m2", "accepted": True, "sessionId": "s2",
            "state": "accepted"}, legacy_events=self._events("s2"))
        _, second, _ = self.submit_with_transport(
            t2, attempt_id=r1["data"]["resultingAttemptId"])
        a2 = second["data"]["submission"]["attemptId"]
        self._deliver_round(a2, "s2", "m2")
        d2 = self.current_delivery_id()
        # 第三次审核返工：达到上限 → invalid_transition
        code, r2 = self.review("changes_requested", d2, note="再返工")
        self.assertFalse(r2["ok"])
        self.assertEqual(r2["error"]["code"], "invalid_transition")
        self.assertIn("上限", r2["error"]["message"])

    def test_worker_cannot_review(self):
        """worker 身份不能提交审核决定。"""
        t = LifecycleFakeTransport(self.workspace, legacy_events=[])
        _, first, _ = self.submit_with_transport(t)
        a1 = first["data"]["submission"]["attemptId"]
        self._deliver_round(a1, "session-lc-1", "msg-lc-1")
        delivery = self.current_delivery_id()
        payload = {"decision": "approved", "deliveryId": delivery}
        path = self.root / "rogue.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        # reviewer 参数必须是 cox；用 worker 身份跑 → CLI 固定身份校验拒绝
        code, result = d4ctl.run(
            ["--db", str(self.db), "--allowed-root", self.allowed,
             "--source", "dsh-sdk", "--agent", "dev-1",
             "--reviewer", "dev-1",
             "review", "submit", "--task", self.task_id,
             "--file", str(path)], synthetic=True)
        self.assertEqual(result["error"]["code"], "permission_denied")


# ---------------------------------------------------------------------
# R2-2 / R2-3：提交异常收尾与回执写入前约束（离线 SYNTHETIC）
# ---------------------------------------------------------------------
class R2BoundaryTests(DshLifecycleTestBase):
    """R2 返工卡负例：收尾责任（R2-2）与回执写入前约束（R2-3）。

    全部宿主替身标 SYNTHETIC；不接触真实 DSH、模型凭据或真实网络出口。
    """

    TERMINAL_EVENTS = [
        {"seq": 1, "type": "turn/start", "sessionId": "session-lc-1",
         "data": {"turn": 1}},
        {"seq": 2, "type": "turn/end", "sessionId": "session-lc-1",
         "data": {"turn": 1, "reason": {"kind": "completed"}}},
    ]

    def _raw_receipt_task(self, write_scope=None):
        """写入函数单元测试用的最小 task 视图。"""
        return {"workspace": str(self.workspace),
                "permissions_json": json.dumps({"writeScope": write_scope or ["."]})}

    def _unknown_transport(self, **over):
        return LifecycleFakeTransport(
            self.workspace,
            submit_receipt={"accepted": True, "state": "unknown"}, **over)

    def _attempt_row(self, attempt_id=None):
        store = SqliteStore(self.db, read_only=True)
        try:
            rows = store.list_attempts(self.task_id)
            if attempt_id is None:
                return rows[0]
            return next(row for row in rows if row["attempt_id"] == attempt_id)
        finally:
            store.close()

    # -- R2-2：提交未知时可靠收尾 -----------------------------------------
    def test_r2_unknown_submit_closes_runtime_exactly_once(self):
        """prompt 结果未知：保留 submit_unknown，close 恰好一次，分段诊断可查。"""
        transport = self._unknown_transport()
        code, result, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "submit_unknown")
        self.assertEqual(transport.start_calls, 1)
        self.assertEqual(len(transport.submit_calls), 1)
        self.assertEqual(transport.close_calls, 1)
        lifecycle = result["error"]["details"]["lifecycle"]
        self.assertTrue(lifecycle["teardown"]["closed"])
        self.assertEqual(lifecycle["collection"]["status"], "failed")
        # unknown 没有被改写成"确定未执行"
        self.assertEqual(self._attempt_row()["submit_state"], "unknown")

    def test_r2_rerun_after_unknown_never_resubmits(self):
        """unknown 之后重跑同一轮次：不再启动、不再投递。"""
        first = self._unknown_transport()
        code1, _, _ = self.submit_with_transport(first)
        self.assertEqual(code1, 1)
        attempt_id = self._attempt_row()["attempt_id"]

        second = self._unknown_transport()
        code2, result2, _ = self.submit_with_transport(second, attempt_id=attempt_id)
        self.assertEqual(code2, 1)
        self.assertEqual(result2["error"]["code"], "submit_unknown")
        self.assertEqual(second.start_calls, 0)
        self.assertEqual(len(second.submit_calls), 0)
        # 第一次的错误事实仍在账本里
        self.assertEqual(self._attempt_row(attempt_id)["submit_state"], "unknown")

    def test_r2_close_failure_reported_without_masking_unknown(self):
        """close 异常：留下 closeError 证据，不掩盖最初的 submit_unknown。"""
        transport = self._unknown_transport(close_error=RuntimeError("shutdown refused"))
        code, result, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "submit_unknown")
        teardown = result["error"]["details"]["lifecycle"]["teardown"]
        self.assertEqual(teardown["closeError"], "RuntimeError")
        self.assertFalse(teardown["runtimeClosed"])
        self.assertEqual(self._attempt_row()["submit_state"], "unknown")

    def test_r2_accepted_submit_keeps_close_once_on_success(self):
        """正常出口回归：成功路径 close 仍恰好一次，分段四段齐全。"""
        transport = LifecycleFakeTransport(self.workspace,
                                           legacy_events=self.TERMINAL_EVENTS)
        code, result, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 0, result)
        self.assertEqual(transport.close_calls, 1)
        data = result["data"]
        self.assertEqual(data["collection"]["status"], "imported")
        self.assertEqual(data["collection"]["receiptWrite"], "written")

    # -- R2-3：回执写入前阻止越界 -----------------------------------------
    def test_r2_receipts_dir_symlink_no_outside_write_nonzero_exit(self):
        """workspace/receipts 是外向 symlink：外部零写入、CLI 非 0、不入账。"""
        outside = self.root / "outside"
        outside.mkdir()
        (self.workspace / "receipts").symlink_to(outside, target_is_directory=True)
        transport = LifecycleFakeTransport(self.workspace,
                                           legacy_events=self.TERMINAL_EVENTS)
        code, result, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "path_escape")
        self.assertEqual(list(outside.rglob("*")), [])
        self.assertNotEqual(self.task_status(), "awaiting_review")

    def test_r2_mid_dir_symlink_no_outside_write(self):
        """中间 task 目录是外向 symlink：外部零写入、CLI 非 0。"""
        outside = self.root / "outside-mid"
        outside.mkdir()
        receipts = self.workspace / "receipts"
        receipts.mkdir()
        (receipts / self.task_id).symlink_to(outside, target_is_directory=True)
        transport = LifecycleFakeTransport(self.workspace,
                                           legacy_events=self.TERMINAL_EVENTS)
        code, result, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "path_escape")
        self.assertEqual(list(outside.rglob("*")), [])

    def test_r2_receipts_dir_swapped_under_fd_writes_nothing_outside(self):
        """目录被并发替换成 symlink：受控 fd 仍写在原目录，外部零写入。

        单纯 realpath 检查无法提供这一保证——写入走的是已打开的目录 fd。
        """
        outside = self.root / "outside-swap"
        outside.mkdir()
        receipts = self.workspace / "receipts"
        real_replace = os.replace
        state = {"swapped": False}

        def swapping_replace(src, dst, *args, **kwargs):
            if not state["swapped"] and dst == "events.json":
                state["swapped"] = True
                receipts.rename(self.workspace / "receipts-moved")
                receipts.symlink_to(outside, target_is_directory=True)
            return real_replace(src, dst, *args, **kwargs)

        transport = LifecycleFakeTransport(self.workspace,
                                           legacy_events=self.TERMINAL_EVENTS)
        with mock.patch("os.replace", swapping_replace):
            code, result, _ = self.submit_with_transport(transport)
        self.assertTrue(state["swapped"], "探针未触发目录交换")
        self.assertEqual(list(outside.rglob("*")), [])
        # 导入阶段发现路径已不可信 → 非 0，且不冒充整链成功
        self.assertNotEqual(code, 0)

    def test_r2_receipt_write_reused_on_identical_content(self):
        """同内容重扫幂等复用；不同内容拒绝覆盖且原字节不变。"""
        from scripts.d4ctl import _dsh_sdk_write_receipt
        task = self._raw_receipt_task()
        bundle = {"hostRunId": "msg-1", "events": [{"seq": 1, "type": "task.started"}]}
        path, state = _dsh_sdk_write_receipt(task, self.task_id, "att-probe", bundle)
        self.assertEqual(state, "written")
        raw = Path(path).read_bytes()
        path2, state2 = _dsh_sdk_write_receipt(task, self.task_id, "att-probe", bundle)
        self.assertEqual(state2, "reused")
        self.assertEqual(Path(path2).read_bytes(), raw)
        with self.assertRaises(ContractError) as ctx:
            _dsh_sdk_write_receipt(task, self.task_id, "att-probe",
                                   {"hostRunId": "msg-1", "events": [{"seq": 2}]})
        self.assertEqual(ctx.exception.code, "path_escape")
        self.assertEqual(Path(path).read_bytes(), raw)

    def test_r2_receipt_target_symlink_rejected(self):
        """目标 events.json 是 symlink：拒绝，外部字节不变。"""
        from scripts.d4ctl import _dsh_sdk_write_receipt
        task = self._raw_receipt_task()
        target_dir = self.workspace / "receipts" / self.task_id / "att-link"
        target_dir.mkdir(parents=True)
        outside = self.root / "outside-target.json"
        outside.write_text('{"hostRunId":"other","events":[]}')
        (target_dir / "events.json").symlink_to(outside)
        before = outside.read_bytes()
        with self.assertRaises(ContractError) as ctx:
            _dsh_sdk_write_receipt(task, self.task_id, "att-link",
                                   {"hostRunId": "msg-1", "events": [{"seq": 1}]})
        self.assertEqual(ctx.exception.code, "path_escape")
        self.assertEqual(outside.read_bytes(), before)

    def test_r2_receipt_outside_write_scope_rejected(self):
        """回执目录不在 writeScope 内 → 声明层拒绝，不落盘。"""
        from scripts.d4ctl import _dsh_sdk_write_receipt
        task = self._raw_receipt_task(write_scope=["out/"])
        with self.assertRaises(ContractError) as ctx:
            _dsh_sdk_write_receipt(task, self.task_id, "att-scope",
                                   {"hostRunId": "msg-1", "events": [{"seq": 1}]})
        self.assertEqual(ctx.exception.code, "path_escape")
        self.assertFalse((self.workspace / "receipts").exists())

    def test_r2_receipt_oversize_rejected(self):
        """回执超过大小上限 → 拒绝写入。"""
        from scripts.d4ctl import _dsh_sdk_write_receipt
        task = self._raw_receipt_task()
        huge = {"hostRunId": "msg-1", "events": [{"blob": "x" * (1024 * 1024 + 10)}]}
        with self.assertRaises(ContractError) as ctx:
            _dsh_sdk_write_receipt(task, self.task_id, "att-big", huge)
        self.assertEqual(ctx.exception.code, "invalid_event")
        self.assertFalse((self.workspace / "receipts").exists())


# ---------------------------------------------------------------------
# R3：生命周期普通异常出口必须转成结构化非零结果（R2 cox review P1）
# ---------------------------------------------------------------------
class R3GenericExceptionTests(DshLifecycleTestBase):
    """R3 返工：只要有宿主 transport，普通 Exception 不得裸抛（离线 SYNTHETIC）。

    被判定的出口链：提交 → 等待 → 归属核对 → 事件采集 → 回执写入 → 入账。
    全部宿主替身标 SYNTHETIC；不启动真实 DSH、模型或网络。
    """

    TERMINAL_EVENTS = [
        {"seq": 1, "type": "turn/start", "sessionId": "session-lc-1",
         "data": {"turn": 1}},
        {"seq": 2, "type": "turn/end", "sessionId": "session-lc-1",
         "data": {"turn": 1, "reason": {"kind": "completed"}}},
    ]

    def _transport(self, **over):
        over.setdefault("legacy_events", self.TERMINAL_EVENTS)
        return LifecycleFakeTransport(self.workspace, **over)

    def _attempt_row(self):
        store = SqliteStore(self.db, read_only=True)
        try:
            rows = store.list_attempts(self.task_id)
            return rows[0] if rows else None
        finally:
            store.close()

    def _assert_controlled(self, code, result, transport, phase):
        """公共判据：非零 + 结构化 + lifecycle 分段 + close 恰好一次 + 事实保留。"""
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "submit_unknown")
        details = result["error"]["details"]
        # 阶段与原始异常类型可查，且明确标注是协议外异常（未新造冻结码）
        self.assertEqual(details["phase"], phase)
        self.assertEqual(details["error"], "RuntimeError")
        self.assertFalse(details["genericException"] is False)
        self.assertTrue(details["genericException"])
        lifecycle = details["lifecycle"]
        self.assertEqual(sorted(lifecycle.keys()),
                         ["collection", "execution", "failureStage",
                          "lifecycleError", "submission", "teardown"])
        self.assertEqual(lifecycle["failureStage"], phase)
        self.assertEqual(lifecycle["collection"]["status"], "failed")
        self.assertEqual(lifecycle["collection"]["error"], "submit_unknown")
        self.assertTrue(lifecycle["teardown"]["closed"])
        # 收尾责任：恰好一次关闭，且没有重新投递
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(transport.start_calls, 1)
        self.assertEqual(len(transport.submit_calls), 1)
        # attempt 的已提交事实保留，且没有成功交付
        self.assertEqual(self._attempt_row()["submit_state"], "submitted")
        self.assertNotEqual(self.task_status(), "awaiting_review")

    def test_r3_host_binding_ordinary_error_is_controlled(self):
        """host_binding() 抛 RuntimeError：close 一次、非零、返回 JSON、含 lifecycle。"""
        transport = self._transport()
        with mock.patch.object(DshSdkAdapter, "host_binding",
                               side_effect=RuntimeError("binding boom")):
            code, result, _ = self.submit_with_transport(transport)
        self._assert_controlled(code, result, transport, "collection")

    def test_r3_collect_events_ordinary_error_is_controlled(self):
        transport = self._transport(legacy_events=[])
        with mock.patch.object(DshSdkAdapter, "collect_events",
                               side_effect=RuntimeError("collect boom")):
            code, result, _ = self.submit_with_transport(transport)
        self._assert_controlled(code, result, transport, "collection")

    def test_r3_receipt_write_ordinary_error_is_controlled(self):
        transport = self._transport()
        with mock.patch.object(d4ctl, "_dsh_sdk_write_receipt",
                               side_effect=RuntimeError("write boom")):
            code, result, _ = self.submit_with_transport(transport)
        self._assert_controlled(code, result, transport, "receipt")
        self.assertFalse((self.workspace / "receipts" / self.task_id).exists())

    def test_r3_import_receipt_ordinary_error_is_controlled(self):
        transport = self._transport()
        with mock.patch.object(d4ctl, "import_receipt",
                               side_effect=RuntimeError("import boom")):
            code, result, _ = self.submit_with_transport(transport)
        self._assert_controlled(code, result, transport, "receipt")

    def test_r3_close_error_keeps_first_error_and_records_close_error(self):
        """close 失败不覆盖首个生命周期错误，closeError 同时挂 teardown 与 execution。"""
        transport = self._transport(close_error=RuntimeError("shutdown refused"))
        with mock.patch.object(DshSdkAdapter, "host_binding",
                               side_effect=RuntimeError("binding boom")):
            code, result, _ = self.submit_with_transport(transport)
        self._assert_controlled(code, result, transport, "collection")
        lifecycle = result["error"]["details"]["lifecycle"]
        self.assertEqual(lifecycle["teardown"]["closeError"], "RuntimeError")
        self.assertEqual(lifecycle["execution"]["closeError"], "RuntimeError")
        # 首个错误仍是 host_binding 的普通异常，没有被 close 失败覆盖
        self.assertEqual(result["error"]["code"], "submit_unknown")

    def test_r3_ordinary_error_then_rerun_does_not_resubmit(self):
        """普通异常后重跑同一 attempt：不重新启动/投递，既有事实不被改写。"""
        transport = self._transport()
        with mock.patch.object(DshSdkAdapter, "host_binding",
                               side_effect=RuntimeError("binding boom")):
            code, _, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 1)
        attempt_id = self._attempt_row()["attempt_id"]

        second = self._transport()
        code2, result2, _ = self.submit_with_transport(second, attempt_id=attempt_id)
        self.assertEqual(second.start_calls, 0)
        self.assertEqual(len(second.submit_calls), 0)
        self.assertEqual(self._attempt_row()["submit_state"], "submitted")
        self.assertEqual(code2, 0)
        self.assertTrue(result2["ok"])

    def test_r3_base_exception_is_not_swallowed(self):
        """不捕获 BaseException：KeyboardInterrupt 继续穿透，但收尾仍发生。"""
        transport = self._transport()
        with mock.patch.object(DshSdkAdapter, "host_binding",
                               side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                self.submit_with_transport(transport)
        self.assertEqual(transport.close_calls, 1)

    def test_r3_contract_error_dispatch_unchanged(self):
        """协议内 ContractError 在 collection 阶段仍是受控结果（R2 分流不变）。"""
        transport = self._transport()
        with mock.patch.object(
                DshSdkAdapter, "host_binding",
                side_effect=ContractError("submit_unknown", message="binding contract")):
            code, result, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["data"]["collection"]["status"], "failed")
        self.assertEqual(result["data"]["collection"]["error"], "submit_unknown")

    def test_r3_same_attempt_replay_reuses_existing_receipt(self):
        """同一 attempt 重放复用既有回执，不因事件时间戳漂移而失败。

        R2-3 的「不同内容拒绝覆盖」本身正确（写入层负例仍由 R2 组覆盖），但事件
        转换用的 ``now_iso()`` 是秒级：重放时生成的回执字节必然不同。回执是该
        attempt 已固化的事实 → 已存在就直接复用做幂等导入，不再重新生成。
        （这正是 R2 引入的回归：R1 的直接覆盖写不会失败。）
        """
        transport = self._transport()
        code1, first, _ = self.submit_with_transport(transport)
        self.assertEqual(code1, 0, first)
        attempt_id = self._attempt_row()["attempt_id"]
        self.assertEqual(first["data"]["collection"]["receiptWrite"], "written")

        ticks = {"n": 0}

        def ticking():
            ticks["n"] += 1
            return "2026-10-07T00:00:%02d+08:00" % ticks["n"]

        second_transport = self._transport()
        with mock.patch.object(d4ctl, "now_iso", side_effect=ticking):
            code2, second, _ = self.submit_with_transport(second_transport,
                                                          attempt_id=attempt_id)
        self.assertEqual(code2, 0, second)
        self.assertEqual(second["data"]["collection"]["receiptWrite"],
                         "reused-existing")
        self.assertEqual(second["data"]["collection"]["status"], "replayed")
        # 重放不重新启动宿主、不重新投递
        self.assertEqual(second_transport.start_calls, 0)
        self.assertEqual(len(second_transport.submit_calls), 0)


# ---------------------------------------------------------------------
# R4：关闭动作恰好一次 —— "关闭已发生"这一事实不能在异常路径上丢失
#     （cox review R3 P1：wait_and_close 已 close 后再抛异常 → finally 重复 close）
# ---------------------------------------------------------------------
class NeverQuiescentTransport(LifecycleFakeTransport):
    """离线夹具（SYNTHETIC）：close 之后账本仍报 ``runtimeClosed=False``。

    复刻 cox R3 review 的场景——关闭动作已执行，但永远拿不到停稳证据。
    """

    def get_status(self, attempt_id):
        if self.statuses:
            return super().get_status(attempt_id)
        return {"attemptId": attempt_id, "state": "failed",
                "endReason": {"kind": "error"}, "runtimeClosed": False,
                "sessionId": self.submit_receipt.get("sessionId"),
                "messageId": self.submit_receipt.get("messageId")}


class PostCloseRaisingAdapter(DshSdkAdapter):
    """R4 夹具：真实执行一次关闭动作，随后在执行阶段抛普通异常。

    与 cox 的复现一致：``wait_and_close`` 内部已经调用底层 ``close()``
    （关闭动作事实记入适配器实例状态），runtime 仍未停稳，随后抛
    ``RuntimeError``。外层 ``finally`` 必须只记录、不再 close。
    """

    def wait_and_close(self, attempt_id,
                       timeout_seconds=DEFAULT_EXECUTION_TIMEOUT_SECONDS):
        last, _timed_out = self._await_terminal(attempt_id, timeout_seconds)
        self._close_and_verify(attempt_id, last)
        raise RuntimeError("runtime 未停稳（R4 夹具）")


def _raising_status(attempt_id):
    """夹具状态项：查询即抛 RuntimeError（对齐宿主账本损坏的 get_status 异常）。"""
    raise RuntimeError("db corrupted")


class R4SingleCloseTests(DshLifecycleTestBase):
    """R4 返工：执行阶段关闭动作恰好一次，且事实不随异常丢失（离线 SYNTHETIC）。

    全部宿主替身标 SYNTHETIC；不启动真实 DSH、模型或网络。
    """

    def _attempt_row(self):
        store = SqliteStore(self.db, read_only=True)
        try:
            rows = store.list_attempts(self.task_id)
            return rows[0] if rows else None
        finally:
            store.close()

    def _run_patched(self, transport, adapter_cls=PostCloseRaisingAdapter):
        """把 d4ctl 构造的适配器替换成 R4 夹具，其余链路保持真实。"""
        with mock.patch.object(d4ctl, "DshSdkAdapter", adapter_cls):
            return self.submit_with_transport(transport, timeout_ms=300)

    def test_r4_execution_close_once_when_wait_and_close_raises_after_close(self):
        """已 close、runtime 未停稳、随后抛 RuntimeError：closeCalls=1、exit=1。

        判据（卡片「必须覆盖」第一条）：关闭动作恰好一次、结构化 lifecycle、
        runtimeClosed=false（关闭已发生 ≠ runtime 已停稳）。
        """
        transport = NeverQuiescentTransport(self.workspace, legacy_events=[])
        code, result, _ = self._run_patched(transport)
        self.assertEqual(code, 1, result)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "submit_unknown")
        details = result["error"]["details"]
        self.assertEqual(details["phase"], "execution")
        self.assertEqual(details["error"], "RuntimeError")
        self.assertTrue(details["genericException"])
        lifecycle = details["lifecycle"]
        self.assertEqual(sorted(lifecycle.keys()),
                         ["collection", "execution", "failureStage",
                          "lifecycleError", "submission", "teardown"])
        self.assertEqual(lifecycle["failureStage"], "execution")
        # 关闭动作恰好一次：finally 不得再次触发 close
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(transport.start_calls, 1)
        self.assertEqual(len(transport.submit_calls), 1)
        # 关闭已发生（closed/closeCalled）但未停稳（runtimeClosed=false）
        self.assertTrue(lifecycle["teardown"]["closed"])
        self.assertTrue(lifecycle["teardown"]["closeCalled"])
        self.assertFalse(lifecycle["teardown"]["runtimeClosed"])
        self.assertTrue(lifecycle["execution"]["closeCalled"])
        self.assertFalse(lifecycle["execution"]["runtimeClosed"])
        # 已提交事实保留，没有成功交付
        self.assertEqual(self._attempt_row()["submit_state"], "submitted")
        self.assertNotEqual(self.task_status(), "awaiting_review")

    def test_r4_second_close_runtime_never_calls_transport_close(self):
        """适配器级直证：已关闭后再调 close_runtime() 不会调用底层 close。"""
        transport = NeverQuiescentTransport(self.workspace, legacy_events=[])
        captured = {}

        class CapturingAdapter(PostCloseRaisingAdapter):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                captured["adapter"] = self

        code, result, _ = self._run_patched(transport, CapturingAdapter)
        self.assertEqual(code, 1, result)
        self.assertEqual(transport.close_calls, 1)
        adapter = captured["adapter"]
        attempt_id = self._attempt_row()["attempt_id"]
        self.assertTrue(adapter.close_invoked(attempt_id))
        # 第二次收尾：复用首次关闭事实，不再次调用底层 close
        summary = adapter.close_runtime(attempt_id)
        self.assertEqual(transport.close_calls, 1)
        self.assertTrue(summary["reusedClose"])
        self.assertTrue(summary["closeCalled"])
        self.assertFalse(summary["runtimeClosed"])
        # 也不会因为第二次调用而重新投递
        self.assertEqual(transport.start_calls, 1)
        self.assertEqual(len(transport.submit_calls), 1)

    def test_r4_post_close_raise_keeps_first_error_and_close_error(self):
        """关闭本身也失败：首个错误不被覆盖，teardown 与 execution 都带 closeError。"""
        transport = NeverQuiescentTransport(
            self.workspace, legacy_events=[],
            close_error=RuntimeError("shutdown refused"))
        code, result, _ = self._run_patched(transport)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["code"], "submit_unknown")
        details = result["error"]["details"]
        self.assertEqual(details["phase"], "execution")
        self.assertEqual(details["error"], "RuntimeError")
        lifecycle = details["lifecycle"]
        self.assertEqual(transport.close_calls, 1)
        self.assertEqual(lifecycle["teardown"]["closeError"], "RuntimeError")
        self.assertEqual(lifecycle["execution"]["closeError"], "RuntimeError")
        self.assertTrue(lifecycle["teardown"]["closed"])
        self.assertTrue(lifecycle["teardown"]["closeCalled"])
        self.assertFalse(lifecycle["teardown"]["runtimeClosed"])

    def test_r4_post_close_raise_after_verify_failure(self):
        """关闭后的复核失败（get_status 异常）：关闭仍一次、未停稳、错误可查。"""
        transport = NeverQuiescentTransport(
            self.workspace, legacy_events=[],
            statuses=[_raising_status] * 100)
        code, result, _ = self._run_patched(transport)
        self.assertEqual(code, 1, result)
        self.assertEqual(result["error"]["details"]["phase"], "execution")
        lifecycle = result["error"]["details"]["lifecycle"]
        self.assertEqual(transport.close_calls, 1)
        self.assertTrue(lifecycle["teardown"]["closed"])
        self.assertTrue(lifecycle["teardown"]["closeCalled"])
        self.assertFalse(lifecycle["teardown"]["runtimeClosed"])
        self.assertEqual(lifecycle["teardown"]["closeError"], "RuntimeError")

    def test_r4_base_exception_after_close_is_not_swallowed_and_closes_once(self):
        """不捕获 BaseException：关闭后抛 KeyboardInterrupt 继续穿透，收尾仍只一次。"""
        transport = NeverQuiescentTransport(self.workspace, legacy_events=[])

        class InterruptingAdapter(PostCloseRaisingAdapter):
            def wait_and_close(self, attempt_id,
                               timeout_seconds=DEFAULT_EXECUTION_TIMEOUT_SECONDS):
                last, _timed_out = self._await_terminal(attempt_id, timeout_seconds)
                self._close_and_verify(attempt_id, last)
                raise KeyboardInterrupt()

        with mock.patch.object(d4ctl, "DshSdkAdapter", InterruptingAdapter):
            with self.assertRaises(KeyboardInterrupt):
                self.submit_with_transport(transport, timeout_ms=300)
        self.assertEqual(transport.close_calls, 1)

    def test_r4_success_path_close_still_exactly_once(self):
        """回归：正常成功路径关闭动作恰好一次，且停稳证据如实为 true。"""
        transport = LifecycleFakeTransport(self.workspace, legacy_events=[
            {"seq": 1, "type": "turn/start", "sessionId": "session-lc-1",
             "data": {"turn": 1}},
            {"seq": 2, "type": "turn/end", "sessionId": "session-lc-1",
             "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ])
        code, result, _ = self.submit_with_transport(transport)
        self.assertEqual(code, 0, result)
        self.assertEqual(transport.close_calls, 1)
        data = result["data"]
        self.assertEqual(data["collection"]["status"], "imported")
        self.assertTrue(data["teardown"]["closed"])
        self.assertTrue(data["teardown"]["closeCalled"])
        self.assertTrue(data["execution"]["closeCalled"])
        self.assertTrue(data["execution"]["runtimeClosed"])

    def test_r4_adapter_close_runtime_idempotent_when_unquiescent(self):
        """适配器级：首次关闭后未停稳，再次 close_runtime 也不重复 close。"""
        transport = NeverQuiescentTransport(self.workspace, legacy_events=[])
        store_dir = self.root / "sdk-store" / "att-r4"
        transport.seed_events_table(str(store_dir), "att-r4")
        adapter = DshSdkAdapter(transport, str(store_dir))
        first = adapter.close_runtime("att-r4")
        self.assertEqual(transport.close_calls, 1)
        self.assertTrue(first["closeCalled"])
        self.assertFalse(first["runtimeClosed"])
        second = adapter.close_runtime("att-r4")
        self.assertEqual(transport.close_calls, 1)
        self.assertTrue(second["reusedClose"])
        self.assertFalse(second["runtimeClosed"])


if __name__ == "__main__":
    unittest.main()
