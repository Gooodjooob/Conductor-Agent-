"""D4-VAL-002-BRIDGE 定向测试：d4ctl 受控 CLI 入口。

关键断言（任务卡 §5）：
- 身份由子命令固定注入：无 --as 参数；worker/source/owner/reviewer 越权一律拒绝
- 事件入口只放行 worker 上报的四种事件；重放/冲突/迟到/终态回退保护继续通过
- relay 只对应最新 changes_requested 的新 attempt；旧 delivery 保留；重复回传拒绝；
  transport 失败保留待发状态，人工重跑可恢复且不重复投递（幂等键保护）
- d4ctl 无身份、错误 task/attempt/source、路径越界全部拒绝
"""
import json
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from scripts import d4ctl
from storage.registry_migrations import apply_registry_candidate_migration
from storage.sqlite_store import SqliteStore

from test_d4_contracts import (
    allowed_roots,
    create_task_payload,
    runtime_root,
    validation_workspace,
)
from test_d4_gateway_adapter import MockTransport, gateway_response
from test_d4_registry_read import SOURCE_DIGEST, _make_legacy, _seed_two_members


class ResponsiveTransport(MockTransport):
    """按请求幂等键自动生成合法回执的 mock（宿主语义：同键返回同 runId）。

    用于自动创建执行轮次的场景（attemptId 调用前未知）；
    queue() 注入的固定项/异常优先返回。
    """

    evidence_level = "LOCAL"

    def call(self, method, params, timeout_ms):
        self.calls.append({"method": method, "params": dict(params),
                           "timeoutMs": timeout_ms})
        if self.responses:
            item = self.responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return gateway_response(params["idempotencyKey"])


def event_payload(task_id, attempt_id, event_type, sequence, **over):
    payload = {
        "eventId": "evt-%s-%03d" % (task_id, sequence),
        "projectId": "conductor",
        "taskId": task_id,
        "attemptId": attempt_id,
        "sourceId": "qclaw-gateway",
        "agentId": "dev-1",
        "sequence": sequence,
        "type": event_type,
        "occurredAt": "2026-09-30T12:00:00+08:00",
        "payload": {},
    }
    payload.update(over)
    return payload


def delivered_payload(task_id, attempt_id, sequence):
    # changedFiles 必须落在任务 writeScope（.conductor-local/d4-real-chain-validation/）内，
    # 否则交付事件校验会以 path_escape 拒绝——这本身就是护栏行为。
    return event_payload(task_id, attempt_id, "task.delivered", sequence, payload={
        "summary": "交付 gateway 桥接切片",
        "reportPath": "%s/report.md" % validation_workspace(),
        "changedFiles": [".conductor-local/d4-real-chain-validation/bridge-002/report.md"],
        "verification": ["定向测试已跑"],
        "risks": [],
        "unfinished": [],
    })


class D4CtlTestCase(unittest.TestCase):
    """公共脚手架：隔离目录 + 独立 db + 受控 workspace。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.root = Path(self.tmp.name).resolve()
        self.db = self.root / "d4ctl.sqlite3"
        self.allowed = str(allowed_roots()[0])
        # workspace 必须真实存在且在隔离根内
        self.workspace = validation_workspace()
        Path(self.workspace).mkdir(parents=True, exist_ok=True)
        Path("%s/report.md" % self.workspace).write_text("# report", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    # -- 基础封装 -----------------------------------------------------------
    def cli(self, argv, transport=None, synthetic=True, agent="dev-1",
            source="qclaw-gateway"):
        base = [
            "--db", str(self.db),
            "--allowed-root", self.allowed,
            "--agent", agent,
            "--source", source,
        ]
        code, result = d4ctl.run(base + argv, transport=transport, synthetic=synthetic)
        return code, result

    def write_json(self, name, payload):
        path = self.root / name
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def create_task(self, transport=None, agent="dev-1", **payload_over):
        payload_over.setdefault("sourceId", "qclaw-gateway")
        payload = create_task_payload(**payload_over)
        file_path = self.write_json("task.json", payload)
        return self.cli(["task", "create", "--file", file_path],
                        transport=transport, agent=agent)

    def submit_first_attempt(self, task_id, transport):
        code, result = self.cli(
            ["attempt", "submit", "--task", task_id], transport=transport)
        assert result["ok"], result
        return result["data"]

    def ingest_events(self, task_id, attempt_id, events, transport=None):
        """按顺序上报事件文件，返回最后一个成功结果。"""
        last = None
        for index, event in enumerate(events):
            file_path = self.write_json("event-%d.json" % index, event)
            code, result = self.cli(["event", "ingest", "--file", file_path],
                                    transport=transport)
            if not result["ok"]:
                return code, result
            last = result
        return last


class D4CtlTaskTests(D4CtlTestCase):
    def test_task_create_ok(self):
        code, result = self.create_task()
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        data = result["data"]
        self.assertIn("taskId", data)
        self.assertEqual(data["status"], "ready")
        self.assertEqual(data["agentId"], "dev-1")

    def test_task_create_rejects_agent_mismatch(self):
        code, result = self.create_task(agentId="dev-2")
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_task_create_rejects_source_mismatch(self):
        code, result = self.create_task(sourceId="qclaw-local")
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_task_create_rejects_missing_file(self):
        code, result = self.cli(["task", "create", "--file", "/nonexistent/task.json"])
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_task_create_rejects_workspace_escape(self):
        code, result = self.create_task(workspace="/etc")
        self.assertEqual(result["error"]["code"], "path_escape")

    def test_query_unknown_task_is_rejected(self):
        # 查询不存在的任务：fail closed，不返回伪造的空对象
        code, result = self.cli(["query", "--task", "task-nope"])
        self.assertFalse(result["ok"])
        self.assertEqual(code, 1)


class D4CtlAttemptSubmitTests(D4CtlTestCase):
    def test_submit_default_attempt_auto_create_and_accept(self):
        # 缺省 --attempt：自动创建执行轮次并提交；ResponsiveTransport 按幂等键回执
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        transport = ResponsiveTransport()
        code, result = self.cli(["attempt", "submit", "--task", task_id], transport=transport)
        self.assertEqual(code, 0)
        data = result["data"]
        attempt_id = data["attemptId"]
        submission = data["submission"]
        self.assertEqual(submission["status"], "accepted_by_host")
        self.assertEqual(submission["host_receipt"], attempt_id)
        self.assertEqual(transport.calls[0]["params"]["idempotencyKey"], attempt_id)
        self.assertEqual(transport.calls[0]["params"]["agentId"], "agent-d4-exec-002")
        # 落库：submit_state=submitted，接收时间非空
        store = SqliteStore(self.db)
        row = store.get_attempt(attempt_id)
        self.assertEqual(row["submit_state"], "submitted")
        self.assertIsNotNone(row["accepted_by_host_at"])
        store.close()

    def test_submit_without_gateway_config_is_rejected(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(["attempt", "submit", "--task", task_id])  # 无 transport/配置
        self.assertEqual(result["error"]["code"], "usage_error")
        self.assertIn("gateway", result["error"]["message"])

    def test_submit_partial_gateway_config_is_rejected(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(["attempt", "submit", "--task", task_id,
                                 "--gateway-bin", "/usr/bin/openclaw-fixture"])
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_submit_relative_gateway_bin_rejected(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(["attempt", "submit", "--task", task_id,
                                 "--gateway-bin", "openclaw",
                                 "--gateway-state-dir", "/tmp/state-fixture"])
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_submit_replays_single_transport_call(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        transport = ResponsiveTransport()
        first = self.cli(["attempt", "submit", "--task", task_id], transport=transport)[1]
        self.assertTrue(first["ok"])
        attempt_id = first["data"]["attemptId"]
        second = self.cli(["attempt", "submit", "--task", task_id,
                           "--attempt", attempt_id], transport=transport)[1]
        self.assertTrue(second["data"]["replayed"])
        self.assertEqual(len(transport.calls), 1)

    def test_submit_fails_closed_on_embedded_receipt(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        transport = ResponsiveTransport()
        transport.queue(gateway_response("whatever", transport="embedded"))
        code, result = self.cli(["attempt", "submit", "--task", task_id], transport=transport)
        self.assertEqual(result["error"]["code"], "host_unconfirmed")
        # 落库为 unknown，不自动重试
        store = SqliteStore(self.db)
        attempt = store.list_attempts(task_id)[-1]
        self.assertEqual(attempt["submit_state"], "unknown")
        self.assertIsNone(attempt["accepted_by_host_at"])
        store.close()

    def test_submit_worker_identity_enforced(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        transport = ResponsiveTransport()
        # 用错误 worker 身份提交 → 越权拒绝，transport 不被调用
        code, result = self.cli(["attempt", "submit", "--task", task_id],
                                transport=transport, agent="qa-1")
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertEqual(len(transport.calls), 0)

    def test_submit_nonexistent_task_rejected(self):
        transport = ResponsiveTransport()
        code, result = self.cli(["attempt", "submit", "--task", "task-nope"],
                               transport=transport)
        self.assertEqual(result["error"]["code"], "invalid_task")
        self.assertEqual(len(transport.calls), 0)


class D4CtlEventTests(D4CtlTestCase):
    def _task_with_accepted_attempt(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        transport = ResponsiveTransport()
        submitted = self.cli(["attempt", "submit", "--task", task_id],
                             transport=transport)[1]
        self.assertTrue(submitted["ok"])
        return task_id, submitted["data"]["attemptId"]

    def test_ingest_started_ok(self):
        task_id, attempt_id = self._task_with_accepted_attempt()
        event = event_payload(task_id, attempt_id, "task.started", 1)
        code, result = self.cli(["event", "ingest", "--file", self.write_json("e.json", event)])
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])

    def test_ingest_rejects_non_whitelisted_type(self):
        task_id, attempt_id = self._task_with_accepted_attempt()
        event = event_payload(task_id, attempt_id, "task.accepted", 1)
        code, result = self.cli(["event", "ingest", "--file", self.write_json("e.json", event)])
        self.assertEqual(result["error"]["code"], "invalid_event")
        self.assertIn("只支持", result["error"]["message"])

    def test_ingest_rejects_source_mismatch(self):
        task_id, attempt_id = self._task_with_accepted_attempt()
        event = event_payload(task_id, attempt_id, "task.started", 1,
                              sourceId="qclaw-local")
        code, result = self.cli(["event", "ingest", "--file", self.write_json("e.json", event)])
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_ingest_rejects_missing_occurred_at(self):
        task_id, attempt_id = self._task_with_accepted_attempt()
        event = event_payload(task_id, attempt_id, "task.started", 1)
        event.pop("occurredAt")
        code, result = self.cli(["event", "ingest", "--file", self.write_json("e.json", event)])
        self.assertEqual(result["error"]["code"], "invalid_event")

    def test_ingest_replay_is_idempotent(self):
        # eventId 去重：同一事件重复上报是幂等重放（ok + replayed 标志），不重复副作用
        task_id, attempt_id = self._task_with_accepted_attempt()
        event = event_payload(task_id, attempt_id, "task.started", 1)
        path = self.write_json("e.json", event)
        first = self.cli(["event", "ingest", "--file", path])[1]
        self.assertTrue(first["ok"])
        second = self.cli(["event", "ingest", "--file", path])[1]
        self.assertTrue(second["ok"])
        self.assertTrue(second["data"]["replayed"])

    def test_ingest_stale_sequence_kept_as_history(self):
        task_id, attempt_id = self._task_with_accepted_attempt()
        newer = event_payload(task_id, attempt_id, "task.progress", 2,
                               payload={"note": "第二步"})
        older = event_payload(task_id, attempt_id, "task.started", 1,
                              occurredAt="2026-09-30T11:00:00+08:00")
        self.assertTrue(self.cli(["event", "ingest", "--file", self.write_json("n.json", newer)])[1]["ok"])
        result = self.cli(["event", "ingest", "--file", self.write_json("o.json", older)])[1]
        self.assertEqual(result["error"]["code"], "stale_sequence")


class D4CtlReviewRelayTests(D4CtlTestCase):
    """G4 回传桥：完整链路 + 防重 + 漂移 + 失败保留待发。"""

    def _setup_reviewable_task(self):
        """创建任务 → 提交 → started → delivered → 返回 taskId/attemptId/deliveryId。"""
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        transport = ResponsiveTransport()
        submitted = self.cli(["attempt", "submit", "--task", task_id],
                             transport=transport)[1]
        self.assertTrue(submitted["ok"])
        attempt_id = submitted["data"]["attemptId"]
        self.ingest_events(task_id, attempt_id, [
            event_payload(task_id, attempt_id, "task.started", 1),
            delivered_payload(task_id, attempt_id, 2),
        ])
        store = SqliteStore(self.db)
        deliveries = store.list_deliveries(task_id)
        store.close()
        self.assertTrue(deliveries)
        return task_id, attempt_id, deliveries[-1]["delivery_id"]

    def _review_changes_requested(self, task_id, delivery_id, note="请补充权限摘要"):
        file_path = self.write_json("review.json", {
            "decision": "changes_requested",
            "deliveryId": delivery_id,
            "note": note,
        })
        code, result = self.cli(["review", "submit", "--task", task_id, "--file", file_path])
        return result["data"]

    def store_current_attempt(self, task_id):
        store = SqliteStore(self.db)
        attempt_id = store.get_task(task_id)["current_attempt_id"]
        store.close()
        return attempt_id

    def test_relay_full_flow_with_new_attempt_and_delivery_preserved(self):
        task_id, attempt_id, delivery_id = self._setup_reviewable_task()
        review = self._review_changes_requested(task_id, delivery_id)
        new_attempt_id = review["resultingAttemptId"]
        self.assertNotEqual(new_attempt_id, attempt_id)
        transport = MockTransport([gateway_response("relay-%s" % review["reviewId"])])
        code, result = self.cli(["review", "relay", "--task", task_id], transport=transport)
        self.assertEqual(code, 0)
        data = result["data"]
        self.assertEqual(data["reviewId"], review["reviewId"])
        self.assertEqual(data["attemptId"], new_attempt_id)
        self.assertEqual(data["runId"], "relay-%s" % review["reviewId"])
        # G4：消息必须包含原 taskId、新 attemptId、返工说明、权限原文摘要
        message = transport.calls[0]["params"]["message"]
        for expected in ("taskId: %s" % task_id,
                         "attemptId: %s" % new_attempt_id,
                         "返工说明: 请补充权限摘要",
                         "writeScope"):
            self.assertIn(expected, message)
        # 旧 delivery 保留；新 attempt 成为当前轮次（直接读库验证，不依赖投影字段名）
        store = SqliteStore(self.db)
        self.assertTrue(store.list_deliveries(task_id))
        task_row = store.get_task(task_id)
        self.assertEqual(task_row["current_attempt_id"], new_attempt_id)
        store.close()
        # activity 里有 relay_sent 且带 synthetic 标记（离线可辨）
        store = SqliteStore(self.db)
        activities = [a for a in store.list_activity(task_id)
                      if a["kind"] == "review.relay_sent"]
        self.assertEqual(len(activities), 1)
        detail_json = json.loads(activities[0]["detail_json"])
        self.assertTrue(detail_json["synthetic"])
        store.close()

    def test_relay_rejected_twice(self):
        task_id, _, delivery_id = self._setup_reviewable_task()
        review = self._review_changes_requested(task_id, delivery_id)
        transport = MockTransport([gateway_response("relay-%s" % review["reviewId"]),
                                   gateway_response("unused")])
        first = self.cli(["review", "relay", "--task", task_id], transport=transport)[1]
        self.assertTrue(first["ok"])
        second = self.cli(["review", "relay", "--task", task_id], transport=transport)[1]
        self.assertEqual(second["error"]["code"], "invalid_transition")
        self.assertIn("已回传", second["error"]["message"])
        self.assertEqual(len(transport.calls), 1)

    def test_relay_records_acceptance_and_allows_second_delivery(self):
        task_id, _, delivery_id = self._setup_reviewable_task()
        review = self._review_changes_requested(task_id, delivery_id)
        transport = ResponsiveTransport()
        code, result = self.cli(["review", "relay", "--task", task_id], transport=transport)
        self.assertEqual(code, 0, result)
        attempt_id = review["resultingAttemptId"]
        result = self.ingest_events(task_id, attempt_id, [
            event_payload(task_id, attempt_id, "task.started", 3),
            delivered_payload(task_id, attempt_id, 4),
        ])
        self.assertTrue(isinstance(result, dict) and result["ok"], result)
        store = SqliteStore(self.db)
        try:
            attempt = store.get_attempt(attempt_id)
            self.assertEqual(attempt["submit_state"], "submitted")
            self.assertTrue(attempt["accepted_by_host_at"])
            self.assertEqual(len(store.list_deliveries(task_id)), 2)
        finally:
            store.close()
        self.assertEqual(len(transport.calls), 1)

    def test_relay_does_not_resend_attempt_submitted_by_normal_entry(self):
        task_id, _, delivery_id = self._setup_reviewable_task()
        review = self._review_changes_requested(task_id, delivery_id)
        transport = ResponsiveTransport()
        code, result = self.cli(["attempt", "submit", "--task", task_id,
                                 "--attempt", review["resultingAttemptId"]], transport=transport)
        self.assertEqual(code, 0, result)
        code, result = self.cli(["review", "relay", "--task", task_id], transport=transport)
        self.assertEqual(result["error"]["code"], "invalid_transition")
        self.assertEqual(len(transport.calls), 1)

    def test_concurrent_relay_uses_one_external_call(self):
        task_id, _, delivery_id = self._setup_reviewable_task()
        self._review_changes_requested(task_id, delivery_id)
        entered, release = threading.Event(), threading.Event()

        class WaitingTransport(ResponsiveTransport):
            def call(self, method, params, timeout_ms):
                entered.set()
                if not release.wait(5):
                    raise RuntimeError("test release timeout")
                return super().call(method, params, timeout_ms)

        transport = WaitingTransport()
        results = []
        first = threading.Thread(target=lambda: results.append(
            self.cli(["review", "relay", "--task", task_id], transport=transport)))
        first.start()
        try:
            self.assertTrue(entered.wait(5))
            _, second = self.cli(["review", "relay", "--task", task_id], transport=transport)
            self.assertEqual(second["error"]["code"], "submit_in_progress")
        finally:
            release.set()
            first.join(5)
        self.assertFalse(first.is_alive())
        self.assertEqual(results[0][0], 0, results)
        self.assertEqual(len(transport.calls), 1)

    def test_relay_recovers_after_activity_failure_without_resending(self):
        task_id, _, delivery_id = self._setup_reviewable_task()
        self._review_changes_requested(task_id, delivery_id)
        transport = ResponsiveTransport()
        original = SqliteStore.log_activity

        def fail_marker(store, task, attempt, kind, detail, at):
            if kind == "review.relay_sent":
                raise RuntimeError("marker persistence failure")
            return original(store, task, attempt, kind, detail, at)

        with mock.patch.object(SqliteStore, "log_activity", fail_marker):
            with self.assertRaisesRegex(RuntimeError, "marker persistence"):
                self.cli(["review", "relay", "--task", task_id], transport=transport)
        code, result = self.cli(["review", "relay", "--task", task_id], transport=transport)
        self.assertEqual(code, 0, result)
        self.assertEqual(len(transport.calls), 1)

    def test_relay_without_changes_requested(self):
        task_id, _, delivery_id = self._setup_reviewable_task()
        file_path = self.write_json("review.json", {
            "decision": "approved", "deliveryId": delivery_id, "note": "通过",
        })
        self.cli(["review", "submit", "--task", task_id, "--file", file_path])
        code, result = self.cli(["review", "relay", "--task", task_id],
                                transport=MockTransport())
        self.assertEqual(result["error"]["code"], "invalid_transition")
        self.assertIn("没有待回传", result["error"]["message"])

    def test_relay_transport_failure_records_unknown_and_blocks_retry(self):
        task_id, _, delivery_id = self._setup_reviewable_task()
        review = self._review_changes_requested(task_id, delivery_id)
        from adapters.qclaw_gateway import GatewayTransportError
        failing = MockTransport([GatewayTransportError("GATEWAY_TIMEOUT")])
        code, result = self.cli(["review", "relay", "--task", task_id], transport=failing)
        self.assertEqual(code, 1)
        self.assertIn("GATEWAY_TIMEOUT", result["error"]["message"])
        # 失败后保留待发：没有 relay_sent 活动
        store = SqliteStore(self.db)
        self.assertEqual([a for a in store.list_activity(task_id)
                          if a["kind"] == "review.relay_sent"], [])
        store.close()
        # Unknown outcome cannot safely be treated as not sent.
        ok_transport = MockTransport([gateway_response("relay-%s" % review["reviewId"])])
        code, retried = self.cli(["review", "relay", "--task", task_id], transport=ok_transport)
        self.assertFalse(retried["ok"])
        self.assertEqual(retried["error"]["code"], "submit_unknown")
        self.assertEqual(len(ok_transport.calls), 0)
        store = SqliteStore(self.db)
        sent = [a for a in store.list_activity(task_id) if a["kind"] == "review.relay_sent"]
        self.assertEqual(len(sent), 0)
        store.close()

    def test_relay_targets_latest_review_when_superseded(self):
        # 后续审核决定覆盖旧的 changes_requested：不再回传被覆盖的返工说明。
        # 注：maxAttempts=2（设计上限），无法二次返工；覆盖场景用 approved 实现。
        task_id, attempt_id, delivery_id = self._setup_reviewable_task()
        self._review_changes_requested(task_id, delivery_id, note="请补充")
        rework_attempt = self.store_current_attempt(task_id)
        # worker 在返工轮交付 → reviewer approved 覆盖 pending
        sub = self.cli(["attempt", "submit", "--task", task_id,
                        "--attempt", rework_attempt],
                       transport=ResponsiveTransport())[1]
        self.assertTrue(sub["ok"])
        self.ingest_events(task_id, rework_attempt, [
            event_payload(task_id, rework_attempt, "task.started", 3),
            delivered_payload(task_id, rework_attempt, 4),
        ])
        store = SqliteStore(self.db)
        delivery2 = store.list_deliveries(task_id)[-1]["delivery_id"]
        store.close()
        fp = self.write_json("review2.json", {
            "decision": "approved", "deliveryId": delivery2, "note": "通过",
        })
        approved = self.cli(["review", "submit", "--task", task_id, "--file", fp])[1]
        self.assertTrue(approved["ok"])
        # 旧 changes_requested 已被 approved 覆盖 → 拒绝回传，不调 transport
        transport = ResponsiveTransport()
        code, result = self.cli(["review", "relay", "--task", task_id], transport=transport)
        self.assertEqual(result["error"]["code"], "invalid_transition")
        self.assertIn("没有待回传", result["error"]["message"])
        self.assertEqual(len(transport.calls), 0)

    def test_relay_after_approval_has_nothing_pending(self):
        # approved 后没有待回传的返工决定 → 拒绝，且不调 transport
        task_id, _, delivery_id = self._setup_reviewable_task()
        file_path = self.write_json("review.json", {
            "decision": "approved", "deliveryId": delivery_id, "note": "通过",
        })
        self.cli(["review", "submit", "--task", task_id, "--file", file_path])
        transport = ResponsiveTransport()
        code, result = self.cli(["review", "relay", "--task", task_id], transport=transport)
        self.assertEqual(result["error"]["code"], "invalid_transition")
        self.assertIn("没有待回传", result["error"]["message"])
        self.assertEqual(len(transport.calls), 0)

    def test_relay_requires_gateway_config(self):
        task_id, _, _ = self._setup_reviewable_task()
        code, result = self.cli(["review", "relay", "--task", task_id])
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_relay_bad_receipt_keeps_pending(self):
        task_id, _, delivery_id = self._setup_reviewable_task()
        review = self._review_changes_requested(task_id, delivery_id)
        transport = MockTransport([gateway_response("relay-wrong-id")])
        code, result = self.cli(["review", "relay", "--task", task_id], transport=transport)
        self.assertEqual(result["error"]["code"], "host_unconfirmed")
        store = SqliteStore(self.db)
        self.assertEqual([a for a in store.list_activity(task_id)
                          if a["kind"] == "review.relay_sent"], [])
        store.close()


class D4CtlGuardTests(D4CtlTestCase):
    """入口护栏：路径/参数/身份边界。"""

    def test_relative_db_rejected(self):
        code, result = d4ctl.run(["--db", "relative.sqlite3",
                                  "--allowed-root", self.allowed,
                                  "query", "--review-queue"])
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_relative_allowed_root_rejected(self):
        code, result = d4ctl.run(["--db", str(self.db),
                                  "--allowed-root", "relative/root",
                                  "query", "--review-queue"])
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_no_identity_override_flag(self):
        # 不存在 --as 任意身份参数：argparse 直接退出
        with self.assertRaises(SystemExit):
            d4ctl.run(["--db", str(self.db), "--allowed-root", self.allowed,
                        "--as", "owner", "query", "--review-queue"])

    def test_no_gateway_agent_flag(self):
        # G1：--gateway-agent 参数已移除；无论传正确映射值还是伪造值，
        # argparse 都直接拒绝（SystemExit），不进入任何身份校验逻辑。
        for value in ("agent-d4-exec-002", "rogue-gateway-agent", "agent-625f3a95"):
            with self.assertRaises(SystemExit):
                d4ctl.run(["--db", str(self.db), "--allowed-root", self.allowed,
                           "--gateway-agent", value, "query", "--review-queue"])

    def test_query_requires_argument(self):
        code, result = self.cli(["query"])
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_query_task_and_activity(self):
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(["query", "--task", task_id])
        # 投影行是 snake_case（store 原始行结构）
        self.assertEqual(result["data"]["task"]["task_id"], task_id)
        code, history = self.cli(["query", "--task", task_id, "--activity"])
        entries = history["data"]["activity"]
        self.assertTrue(any(item.get("kind") == "task.created" for item in entries))

    def test_query_review_queue(self):
        SqliteStore(self.db).close()
        code, result = self.cli(["query", "--review-queue"])
        self.assertEqual(code, 0)

    def test_query_does_not_bootstrap_registry(self):
        # query 是只读入口：首次查询也不能偷偷登记来源或成员。
        SqliteStore(self.db).close()
        before = self.db.read_bytes()
        code, result = d4ctl.run(["--db", str(self.db),
                                  "--allowed-root", self.allowed,
                                  "query", "--review-queue"])
        self.assertEqual(code, 0)
        self.assertEqual(before, self.db.read_bytes())
        store = SqliteStore(self.db)
        self.assertIsNone(store.get_source("qclaw-gateway"))
        self.assertIsNone(store.get_agent("dev-1"))
        self.assertIsNone(store.get_agent("cox"))
        store.close()

    def test_query_missing_database_does_not_create_it(self):
        code, result = self.cli(["query", "--review-queue"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "usage_error")
        self.assertFalse(self.db.exists())

    def test_cli_rejects_identity_override(self):
        for option, value in (("--source", "rogue-source"),
                              ("--agent", "rogue-agent"),
                              ("--reviewer", "rogue-reviewer")):
            code, result = d4ctl.run([
                "--db", str(self.db), "--allowed-root", self.allowed,
                option, value, "query", "--review-queue",
            ])
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "permission_denied")


class D4CtlDshSdkRouteTests(D4CtlTestCase):
    """D4-DSH-02-SDK-001 §5.3：DSH SDK opt-in 路由与互斥/来源匹配。

    生产路由默认禁用（fail closed），本组全部为拒绝路径回归；
    真实投递路径由后续运行隔离实施卡授权。原 Gateway/WorkBuddy 路由
    回归见各自测试组（本组不改动其行为）。
    R1 返工新增定向测试见 D4CtlDshSdkR1Tests（argv/策略材料/版本预检）。
    """

    def setUp(self):
        super().setUp()
        # 隔离目录里的假 bin/home：路径校验用，绝不执行（授权门禁在前）。
        self.sdk_home = self.root / "dsh-home"
        self.sdk_home.mkdir()
        self.sdk_bin = self.root / "dsh-bin"
        self.sdk_bin.write_text("#!/bin/sh\n", encoding="utf-8")

    def _dsh_route(self, task_id, source="dsh-sdk", **extra):
        argv = ["attempt", "submit", "--task", task_id,
                "--dsh-sdk-bin", str(self.sdk_bin),
                "--dsh-sdk-home", str(self.sdk_home)] + list(extra)
        return self.cli(argv, source=source) if hasattr(self, "cli") else (None, None)

    def _create_dsh_task(self):
        payload = create_task_payload(sourceId="dsh-sdk")
        file_path = self.write_json("dsh-task.json", payload)
        return self.cli(["task", "create", "--file", file_path], source="dsh-sdk")

    def test_dsh_source_accepted_in_whitelist(self):
        # dsh-sdk 在受控来源白名单内：建卡成功（不需要路由参数）
        code, result = self._create_dsh_task()
        self.assertEqual(code, 0, result)
        self.assertTrue(result["ok"])

    def test_dsh_route_requires_matching_source(self):
        # DSH 路由 + qclaw-gateway 来源 → 拒绝
        _, created = self._create_dsh_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(
            ["attempt", "submit", "--task", task_id,
             "--dsh-sdk-bin", str(self.sdk_bin),
             "--dsh-sdk-home", str(self.sdk_home)], source="qclaw-gateway")
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("dsh-sdk", result["error"]["message"])

    def test_dsh_source_requires_dsh_route(self):
        # dsh-sdk 来源但没有 --dsh-sdk-* 参数 → 拒绝（且发生在授权门禁前）
        _, created = self._create_dsh_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(["attempt", "submit", "--task", task_id],
                                source="dsh-sdk")
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("路由", result["error"]["message"])

    def test_dsh_route_mutually_exclusive_with_workbuddy(self):
        _, created = self._create_dsh_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(
            ["attempt", "submit", "--task", task_id,
             "--dsh-sdk-bin", str(self.sdk_bin),
             "--dsh-sdk-home", str(self.sdk_home),
             "--workbuddy-bin", "/usr/bin/true",
             "--workbuddy-home", str(self.root)], source="dsh-sdk")
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("互斥", result["error"]["message"])

    def test_dsh_route_mutually_exclusive_with_gateway(self):
        _, created = self._create_dsh_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(
            ["attempt", "submit", "--task", task_id,
             "--dsh-sdk-bin", str(self.sdk_bin),
             "--dsh-sdk-home", str(self.sdk_home),
             "--gateway-bin", "/usr/bin/true",
             "--gateway-state-dir", str(self.root)], source="dsh-sdk")
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("互斥", result["error"]["message"])

    def test_dsh_production_routing_disabled_by_default(self):
        """未设 D4_DSH_SDK_PROD_ROUTING=1 → permission_denied，不启动任务 SDK。"""
        _, created = self._create_dsh_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(
            ["attempt", "submit", "--task", task_id,
             "--dsh-sdk-bin", str(self.sdk_bin),
             "--dsh-sdk-home", str(self.sdk_home)], source="dsh-sdk")
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("未授权", result["error"]["message"])

    def test_dsh_partial_config_rejected(self):
        _, created = self._create_dsh_task()
        task_id = created["data"]["taskId"]
        code, result = self.cli(
            ["attempt", "submit", "--task", task_id,
             "--dsh-sdk-bin", str(self.sdk_bin)], source="dsh-sdk")
        # 授权门禁先于配置完整性（fail closed 顺序：先策略后参数）
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_gateway_route_regression_unchanged(self):
        """原 Gateway 路由回归：默认来源 + gateway 参数仍走 gateway 通道。"""
        _, created = self.create_task()
        task_id = created["data"]["taskId"]
        transport = ResponsiveTransport()
        code, result = self.cli(["attempt", "submit", "--task", task_id],
                                transport=transport)
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["submission"]["status"], "accepted_by_host")
        self.assertEqual(len(transport.calls), 1)


class D4CtlDshSdkR1Tests(D4CtlTestCase):
    """R1 返工卡定向测试：R1-1 argv profile / R1-3 策略材料门禁 / R1-6 版本预检前置。

    全部离线（SYNTHETIC/LOCAL-CLI-PARSE-ONLY）；不启动真实 SDK、模型或 Agent。

    R2 说明：R2-1 之后真实路由**无条件** fail closed，因此"材料齐全 → 版本预检"
    这条 CLI 路径不再可达；前置版本拒绝的行为改由 R2 组的单元级回归覆盖
    （`_dsh_sdk_version_preflight` 本体未变）。
    """

    def setUp(self):
        super().setUp()
        self.sdk_home = self.root / "dsh-home"
        self.sdk_home.mkdir()
        self.sdk_bin = self.root / "dsh-bin"
        self.sdk_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        payload = create_task_payload(sourceId="dsh-sdk")
        self.task_id = self.cli(
            ["task", "create", "--file", self.write_json("t.json", payload)],
            source="dsh-sdk")[1]["data"]["taskId"]

    def _write_materials(self, wrong_bin=None):
        """写三份受控策略材料（假材料仅用于触发门禁后的下一步拒绝）。"""
        member = self.root / "member.json"
        member.write_text(json.dumps({"agents": ["dsh-sdk-exec-001"]}),
                          encoding="utf-8")
        isolation = self.root / "isolation.json"
        isolation.write_text(json.dumps({"isolationLevel": "sandboxed"}),
                              encoding="utf-8")
        network = self.root / "network.json"
        network.write_text(json.dumps({"allowModelNetwork": True,
                                       "allowToolNetwork": True}),
                           encoding="utf-8")
        return {"D4_DSH_SDK_PROD_ROUTING": "1",
                "D4_DSH_SDK_MEMBER_MANIFEST": str(member),
                "D4_DSH_SDK_ISOLATION_MANIFEST": str(isolation),
                "D4_DSH_SDK_NETWORK_MANIFEST": str(network)}

    def _run_dsh(self, env, bin_path=None, task_id=None):
        import os
        from unittest import mock as _mock
        argv = ["attempt", "submit", "--task", task_id or self.task_id,
                "--dsh-sdk-bin", str(bin_path or self.sdk_bin),
                "--dsh-sdk-home", str(self.sdk_home)]
        with _mock.patch.dict(os.environ, env, clear=False):
            return d4ctl.run(
                ["--db", str(self.db), "--allowed-root", self.allowed,
                 "--source", "dsh-sdk", "--agent", "dev-1"] + argv)

    def _attempts(self, task_id):
        store = SqliteStore(self.db, read_only=True)
        try:
            return store.list_attempts(task_id)
        finally:
            store.close()

    # -- R1-1：argv 固定 --profile sdk-minimal ---------------------------
    def test_r1_launch_argv_carries_fixed_profile(self):
        """真实启动 argv 必须含 --profile sdk-minimal（R1-1/cox review F1）。"""
        from scripts.d4ctl import _resolve_dsh_sdk_base, _resolve_dsh_sdk_store
        task = {"task_id": "task-x", "workspace": str(self.root)}
        args = type("A", (), {"dsh_sdk_bin": str(self.sdk_bin),
                              "dsh_sdk_home": str(self.sdk_home)})()
        command, workspace, env = _resolve_dsh_sdk_base(args, task)
        # argv 形状：node bin --profile sdk-minimal（受控常量）
        self.assertEqual(command[-2:], ["--profile", "sdk-minimal"])
        self.assertNotIn("--profile", command[:-2])
        self.assertEqual(env["DSH_PROFILE"], "sdk-minimal")
        store_command, sdk_store = _resolve_dsh_sdk_store(
            type("A", (), {"dsh_sdk_bin": str(self.sdk_bin),
                           "dsh_sdk_home": str(self.sdk_home),
                           "task": "task-x"})(), "att-x")
        self.assertEqual(store_command, command)
        # R1-4：store 按轮次分目录
        self.assertTrue(sdk_store.endswith("d4-dsh-sdk-store/task-x/att-x"))

    def test_r1_store_rejects_slash_attempt_id(self):
        """attemptId 含路径分隔符 → 拒绝（防目录注入）。"""
        from scripts.d4ctl import _resolve_dsh_sdk_store
        args = type("A", (), {"dsh_sdk_bin": str(self.sdk_bin),
                              "dsh_sdk_home": str(self.sdk_home),
                              "task": "task-x"})()
        with self.assertRaises(ValueError):
            _resolve_dsh_sdk_store(args, "../escape")

    def test_r1_profile_not_injectable_via_cli(self):
        """CLI 不提供 profile 参数：--dsh-sdk-profile 直接被 argparse 拒绝（exit 2）。"""
        with self.assertRaises(SystemExit) as ctx:
            d4ctl.run(
                ["--db", str(self.db), "--allowed-root", self.allowed,
                 "--source", "dsh-sdk", "--agent", "dev-1",
                 "attempt", "submit", "--task", self.task_id,
                 "--dsh-sdk-bin", str(self.sdk_bin),
                 "--dsh-sdk-home", str(self.sdk_home),
                 "--dsh-sdk-profile", "headless"])
        self.assertEqual(ctx.exception.code, 2)

    # -- R1-3：策略材料门禁（env=1 不够）--------------------------------
    def test_r1_env_one_without_materials_rejected_zero_start(self):
        """D4_DSH_SDK_PROD_ROUTING=1 但缺三份策略材料 → 拒绝且零占位。"""
        code, result = self._run_dsh({"D4_DSH_SDK_PROD_ROUTING": "1"})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("材料", result["error"]["message"])
        self.assertEqual(len(self._attempts(self.task_id)), 0)

    def test_r1_env_one_with_partial_materials_rejected(self):
        """只给成员登记、缺隔离/网络材料 → 仍拒绝。"""
        member = self.root / "member.json"
        member.write_text(json.dumps({"agents": ["dsh-sdk-exec-001"]}),
                          encoding="utf-8")
        code, result = self._run_dsh({
            "D4_DSH_SDK_PROD_ROUTING": "1",
            "D4_DSH_SDK_MEMBER_MANIFEST": str(member)})
        self.assertEqual(result["error"]["code"], "permission_denied")

    def test_r1_env_one_with_full_materials_rejected_by_r2_gate(self):
        """三份材料齐全 + env=1 → **仍然拒绝**（R2-1 取代 R1-3 的"齐全即放行"）。

        这正是 R1 cox 审核 F1 的负例：自填 JSON 不能打开真实路由。
        拒绝发生在创建轮次之前 → 零 attempt、零 SDK 启动、零 prompt。
        """
        code, result = self._run_dsh(self._write_materials(), bin_path=self.sdk_bin)
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("非授权材料", result["error"]["message"])
        self.assertEqual(len(self._attempts(self.task_id)), 0)

    def test_r1_gate_material_malformed_json_rejected(self):
        """材料文件 JSON 不合法 → 拒绝。"""
        member = self.root / "member-bad.json"
        member.write_text("not-json{", encoding="utf-8")
        code, result = self._run_dsh({
            "D4_DSH_SDK_PROD_ROUTING": "1",
            "D4_DSH_SDK_MEMBER_MANIFEST": str(member)})
        self.assertEqual(result["error"]["code"], "permission_denied")


class D4CtlDshSdkR2Tests(D4CtlTestCase):
    """R2 返工卡定向测试：R2-1 真实路由无条件 fail closed；R1-6 版本预检单元级回归。

    全部离线（SYNTHETIC）；不启动真实 SDK、模型或 Agent，不接触真实网络出口。
    """

    def setUp(self):
        super().setUp()
        self.sdk_home = self.root / "dsh-home"
        self.sdk_home.mkdir()
        self.sdk_bin = self.root / "dsh-bin"
        self.sdk_bin.write_text("#!/bin/sh\n", encoding="utf-8")
        payload = create_task_payload(sourceId="dsh-sdk")
        self.task_id = self.cli(
            ["task", "create", "--file", self.write_json("t.json", payload)],
            source="dsh-sdk")[1]["data"]["taskId"]

    def _write_materials(self):
        """三份形状完全正确的自填材料（R2-1：形状正确也不构成授权）。"""
        member = self.root / "member.json"
        member.write_text(json.dumps({"agents": ["dsh-sdk-exec-001"]}),
                          encoding="utf-8")
        isolation = self.root / "isolation.json"
        isolation.write_text(json.dumps({"isolationLevel": "sandboxed"}),
                             encoding="utf-8")
        network = self.root / "network.json"
        network.write_text(json.dumps({"allowModelNetwork": True,
                                       "allowToolNetwork": True}),
                           encoding="utf-8")
        return {"D4_DSH_SDK_PROD_ROUTING": "1",
                "D4_DSH_SDK_MEMBER_MANIFEST": str(member),
                "D4_DSH_SDK_ISOLATION_MANIFEST": str(isolation),
                "D4_DSH_SDK_NETWORK_MANIFEST": str(network)}

    def _run_dsh(self, env, task_id=None):
        import os
        from unittest import mock as _mock
        argv = ["attempt", "submit", "--task", task_id or self.task_id,
                "--dsh-sdk-bin", str(self.sdk_bin),
                "--dsh-sdk-home", str(self.sdk_home)]
        with _mock.patch.dict(os.environ, env, clear=False):
            return d4ctl.run(
                ["--db", str(self.db), "--allowed-root", self.allowed,
                 "--source", "dsh-sdk", "--agent", "dev-1"] + argv)

    def _attempts(self):
        store = SqliteStore(self.db, read_only=True)
        try:
            return store.list_attempts(self.task_id)
        finally:
            store.close()

    # -- R2-1：真实路由无条件 fail closed ----------------------------------
    def test_r2_self_written_materials_cannot_open_real_route(self):
        """R2-1 核心负例：env=1 + 三份自填 JSON 齐全 → 仍 permission_denied。

        零创建 attempt、零启动 SDK、零 prompt；材料形状解析只进诊断。
        """
        code, result = self._run_dsh(self._write_materials())
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("非授权材料", result["error"]["message"])
        self.assertEqual(len(self._attempts()), 0)
        gate = result["error"]["details"]["gate"]
        self.assertEqual(gate["authorizationSource"], "none-in-this-card")
        self.assertTrue(gate["diagnostics"]["untrusted"])
        # 形状可解析 ≠ 可信：三份材料全部 shape-ok，路由照样拒绝
        statuses = {k: v["status"] for k, v in gate["diagnostics"]["materials"].items()}
        self.assertEqual(statuses, {"member": "shape-ok", "isolation": "shape-ok",
                                    "network": "shape-ok"})

    def test_r2_gate_rejects_even_without_env(self):
        """未设 env 时的默认拒绝路径语义保持（原 R1-3 回归）。"""
        code, result = self._run_dsh({})
        self.assertEqual(result["error"]["code"], "permission_denied")
        self.assertIn("未授权", result["error"]["message"])
        self.assertEqual(len(self._attempts()), 0)

    def test_r2_gate_error_does_not_advertise_json_as_enablement(self):
        """R2-1：错误信息不得暗示"补齐三份 JSON 就能启用生产"。"""
        _, result = self._run_dsh(self._write_materials())
        details = result["error"]["details"]
        # 不再有"必需材料/必需环境变量"这种就地补齐清单
        self.assertNotIn("requiredMaterials", details)
        self.assertNotIn("requiredEnv", details)
        gate = details["gate"]
        self.assertEqual(gate["gate"], "dsh-sdk-prod-routing-fail-closed")
        # 放行前提一律表述为"另卡"事项，不是当前可补齐的开关
        self.assertTrue(all(item.startswith("另卡：")
                            for item in gate["futureCardRequirements"]))
        # 材料内容（如 isolationLevel 的取值）不回流进错误详情，只留形状状态
        self.assertNotIn("sandboxed", json.dumps(gate, ensure_ascii=False))

    # -- R1-6 版本预检：单元级回归（CLI 真实路由已 fail closed）------------
    def test_r2_version_preflight_rejects_bad_bin_before_placeholder(self):
        """前置版本拒绝行为保留：bin 非零退出 → ValueError + 未创建提交占位。"""
        import sys
        from scripts.d4ctl import _dsh_sdk_version_preflight
        bad = self.root / "bad-bin.py"
        bad.write_text("import sys\nsys.exit(7)\n", encoding="utf-8")
        command = [sys.executable, str(bad)]
        env = {"HOME": str(self.root), "PATH": "/usr/bin:/bin"}
        with self.assertRaises(ValueError) as ctx:
            _dsh_sdk_version_preflight(command, str(self.root), env)
        self.assertIn("未创建提交占位", str(ctx.exception))
        self.assertEqual(len(self._attempts()), 0)

    def test_r2_version_preflight_rejects_wrong_version(self):
        """bin 输出不支持版本 → ValueError（CLI 映射 usage_error），零占位。"""
        import sys
        from scripts.d4ctl import _dsh_sdk_version_preflight
        wrong = self.root / "wrong-bin.py"
        wrong.write_text("import sys\nsys.stdout.write('0.1.1-rc.2\\n')\n",
                         encoding="utf-8")
        command = [sys.executable, str(wrong)]
        env = {"HOME": str(self.root), "PATH": "/usr/bin:/bin"}
        with self.assertRaises(ValueError) as ctx:
            _dsh_sdk_version_preflight(command, str(self.root), env)
        self.assertIn("不受支持", str(ctx.exception))
        self.assertEqual(len(self._attempts()), 0)

    def test_r2_version_preflight_accepts_supported_baseline(self):
        """支持基线精确匹配才通过（权威判据未变）。"""
        import sys
        from scripts.d4ctl import _dsh_sdk_version_preflight
        good = self.root / "good-bin.py"
        good.write_text("import sys\nsys.stdout.write('0.2.0-rc.2\\n')\n",
                        encoding="utf-8")
        command = [sys.executable, str(good)]
        env = {"HOME": str(self.root), "PATH": "/usr/bin:/bin"}
        self.assertEqual(_dsh_sdk_version_preflight(command, str(self.root), env),
                         "0.2.0-rc.2")


class D4CtlAgentRegistryTests(D4CtlTestCase):
    """agent list / agent inspect 只读入口：复用既有只读服务，fail closed。

    只打开已存在数据库（mode=ro 裸连接，无 PRAGMA 赋值）；未迁移库、缺失
    成员、坏过滤参数按服务稳定错误语义拒绝，不伪装成空结果；前后数据库
    字节不变，且不登记固定 source/agent。registryId 显式传入或由既有只读
    inspect_registry_schema 取得，两条路径都交给同一只读服务校验。
    """

    def setUp(self):
        super().setUp()
        self.registry_id = "reg-1"

    def _migrated_db(self, with_members=True):
        """legacy 库 + 显式候选迁移（+ 可选两个合成成员），测试数据非业务事实。"""
        store = _make_legacy(str(self.db))
        apply_registry_candidate_migration(
            store.conn, registry_id=self.registry_id, source_digest=SOURCE_DIGEST)
        if with_members:
            _seed_two_members(store.conn)
        store.close()
        return self.db

    # -- list 正常路径 ------------------------------------------------------
    def test_agent_list_json_full_projection(self):
        self._migrated_db()
        code, result = self.cli(["agent", "list", "--registry", self.registry_id,
                                 "--format", "json"])
        self.assertEqual(code, 0, result)
        data = result["data"]
        self.assertEqual(data["schemaVersion"], "registry-agent-list.v1")
        self.assertEqual(data["registry"]["registryId"], self.registry_id)
        self.assertEqual(data["registry"]["authority"], "candidate")
        self.assertFalse(data["wouldWrite"])
        self.assertFalse(data["wouldDispatch"])
        self.assertTrue(data["authorityUnchanged"])
        self.assertEqual(data["filters"],
                         {"host": None, "role": None, "enabled": None})
        # 全体成员先完整校验再过滤；列表按 agentId 码点升序（服务冻结口径）。
        self.assertEqual([item["agentId"] for item in data["items"]],
                         ["cox-arch", "dev-wkb1"])
        first = data["items"][0]
        self.assertEqual(first["environment"]["hostType"], "codex")
        self.assertTrue(any(check["checkId"] == "chk-cox"
                            for check in first["checks"]))

    def test_agent_list_filters(self):
        self._migrated_db()
        cases = [
            (["--host", "workbuddy"], ["dev-wkb1"]),
            (["--host", "codex"], ["cox-arch"]),
            (["--role", "skill-worker"], ["dev-wkb1"]),
            (["--role", "sales-architect"], ["cox-arch"]),
            (["--enabled", "true"], ["dev-wkb1"]),
            (["--enabled", "false"], ["cox-arch"]),
        ]
        for extra, expected in cases:
            code, result = self.cli(
                ["agent", "list", "--registry", self.registry_id,
                 "--format", "json"] + extra)
            self.assertEqual(code, 0, result)
            self.assertEqual(
                [item["agentId"] for item in result["data"]["items"]], expected)
        # 过滤条件回显进 filters 投影。
        code, result = self.cli(["agent", "list", "--registry", self.registry_id,
                                 "--host", "workbuddy", "--enabled", "true"])
        self.assertEqual(result["data"]["filters"],
                         {"host": "workbuddy", "role": None, "enabled": True})

    def test_agent_list_empty_registry(self):
        self._migrated_db(with_members=False)
        code, result = self.cli(["agent", "list", "--registry", self.registry_id,
                                 "--format", "json"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["items"], [])
        # 空列表在 table 模式同样正常显示，不是错误也不是伪装。
        code, result = self.cli(["agent", "list", "--registry", self.registry_id,
                                 "--format", "table"])
        self.assertEqual(code, 0, result)
        self.assertIn("members: 0", result["data"]["table"])

    # -- list 过滤参数校验（转交服务，fail closed） ---------------------------
    def test_agent_list_rejects_bad_filter_values(self):
        self._migrated_db()
        # enabled 只接受小写 true/false：argparse 直接拒绝，不进服务。
        for bad in ("True", "1", "yes"):
            with self.assertRaises(SystemExit):
                self.cli(["agent", "list", "--registry", self.registry_id,
                          "--enabled", bad])
        # host/role 校验转交服务：稳定 invalid_argument，不猜宿主/角色。
        for extra, field in ((["--host", "not a host"], "host"),
                             (["--role", "boss"], "role")):
            code, result = self.cli(
                ["agent", "list", "--registry", self.registry_id] + extra)
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "invalid_task")
            self.assertEqual(result["error"]["details"]["field"], field)
            self.assertEqual(result["error"]["details"]["reason"],
                             "invalid_argument")
        # registryId 与库内元数据不一致：稳定拒绝，不回退成全库列表。
        code, result = self.cli(["agent", "list", "--registry", "reg-other"])
        self.assertEqual(result["error"]["code"], "invalid_task")
        self.assertEqual(result["error"]["details"]["field"], "registryId")
        self.assertEqual(result["error"]["details"]["reason"],
                         "registry_id_mismatch")

    # -- table 模式 ---------------------------------------------------------
    def test_agent_list_table_readable_without_derived_state(self):
        self._migrated_db()
        code, result = self.cli(["agent", "list", "--registry", self.registry_id,
                                 "--format", "table"])
        self.assertEqual(code, 0, result)
        table = result["data"]["table"]
        # 完整服务投影仍随行返回，不因 table 渲染丢失。
        self.assertEqual(result["data"]["result"]["schemaVersion"],
                         "registry-agent-list.v1")
        self.assertIn("dev-wkb1", table)
        self.assertIn("chk-old", table)          # 检查记录逐条显示
        self.assertIn("宿主身份未确认", table)     # failureReason 不丢失
        self.assertIn("members: 2", table)
        # 不推导 online / ready / 可接任务状态。
        self.assertNotIn("online", table)
        self.assertNotIn("ready", table)

    # -- inspect ------------------------------------------------------------
    def test_agent_inspect_full_detail_projection(self):
        self._migrated_db()
        code, result = self.cli(["agent", "inspect", "dev-wkb1",
                                 "--registry", self.registry_id])
        self.assertEqual(code, 0, result)
        data = result["data"]
        self.assertEqual(data["schemaVersion"], "registry-agent-detail.v1")
        self.assertFalse(data["wouldWrite"])
        self.assertFalse(data["wouldDispatch"])
        self.assertTrue(data["authorityUnchanged"])
        self.assertEqual(data["item"]["agentId"], "dev-wkb1")
        self.assertEqual(data["item"]["environment"]["hostType"], "workbuddy")
        # 检查按 observedAt 原字符串降序（服务冻结口径）。
        self.assertEqual([check["checkId"] for check in data["item"]["checks"]],
                         ["chk-current", "chk-expired", "chk-old"])
        self.assertEqual([row["bindingRevision"] for row in data["bindingHistory"]],
                         [1])
        self.assertEqual([row["environmentId"] for row in data["environmentHistory"]],
                         ["env-1"])
        self.assertEqual([row["modelProfileId"] for row in data["modelProfiles"]],
                         ["mp-1"])

    def test_agent_inspect_missing_member_rejected(self):
        self._migrated_db()
        code, result = self.cli(["agent", "inspect", "ghost-1",
                                 "--registry", self.registry_id])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "invalid_task")
        self.assertEqual(result["error"]["details"]["field"], "agentId")
        self.assertEqual(result["error"]["details"]["reason"],
                         "missing_reference")

    # -- 缺省 registryId：只读发现（不猜、不建库、fail closed） -----------------
    def test_agent_registry_id_discovered_when_registry_omitted(self):
        """不传 --registry 时，只用既有只读 schema 检查取得库内 registryId。"""
        self._migrated_db()
        code, result = self.cli(["agent", "list", "--host", "codex",
                                 "--format", "json"])
        self.assertEqual(code, 0, result)
        data = result["data"]
        self.assertEqual(data["registry"]["registryId"], self.registry_id)
        self.assertEqual([item["agentId"] for item in data["items"]], ["cox-arch"])
        code, result = self.cli(["agent", "list", "--format", "table"])
        self.assertEqual(code, 0, result)
        self.assertIn("members: 2", result["data"]["table"])
        code, result = self.cli(["agent", "inspect", "dev-wkb1"])
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["schemaVersion"], "registry-agent-detail.v1")
        self.assertEqual(result["data"]["item"]["agentId"], "dev-wkb1")

    def test_agent_discovery_fails_closed_on_unmigrated_database(self):
        """缺省发现同样不迁移、不猜测：未迁移库 → registry_unmigrated。"""
        SqliteStore(str(self.db)).close()
        for argv in (["agent", "list", "--format", "json"],
                     ["agent", "inspect", "dev-wkb1"]):
            code, result = self.cli(argv)
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "invalid_task")
            self.assertEqual(result["error"]["details"]["field"], "registry")
            self.assertEqual(result["error"]["details"]["reason"],
                             "registry_unmigrated")

    # -- 数据库边界（不创建、未迁移拒绝、前后不变） ----------------------------
    def test_agent_missing_database_not_created(self):
        for argv in (["agent", "list", "--registry", self.registry_id],
                     ["agent", "list", "--registry", self.registry_id,
                      "--format", "table"],
                     ["agent", "inspect", "dev-wkb1",
                      "--registry", self.registry_id]):
            code, result = self.cli(argv)
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "usage_error")
            self.assertIn("不存在", result["error"]["message"])
        self.assertFalse(self.db.exists())

    def test_agent_unmigrated_database_rejected(self):
        # 有 legacy 库但没有候选 registry 表：稳定 registry_unmigrated，
        # 不自动迁移、不伪装成空列表。
        SqliteStore(str(self.db)).close()
        for argv in (["agent", "list", "--registry", self.registry_id],
                     ["agent", "inspect", "dev-wkb1",
                      "--registry", self.registry_id]):
            code, result = self.cli(argv)
            self.assertEqual(code, 1)
            self.assertEqual(result["error"]["code"], "invalid_task")
            self.assertEqual(result["error"]["details"]["reason"],
                             "registry_unmigrated")

    def test_agent_read_only_database_unchanged(self):
        self._migrated_db()
        before = self.db.read_bytes()
        for argv in (["agent", "list", "--registry", self.registry_id,
                      "--format", "json"],
                     ["agent", "list", "--registry", self.registry_id,
                      "--format", "table"],
                     ["agent", "inspect", "dev-wkb1",
                      "--registry", self.registry_id]):
            code, result = self.cli(argv)
            self.assertEqual(code, 0, result)
        self.assertEqual(before, self.db.read_bytes())
        # 只读入口不登记固定 source/agent（与 query 同一护栏口径）。
        store = SqliteStore(self.db)
        try:
            self.assertIsNone(store.get_source("qclaw-gateway"))
            self.assertIsNone(store.get_agent("dev-1"))
            self.assertIsNone(store.get_agent("cox"))
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
