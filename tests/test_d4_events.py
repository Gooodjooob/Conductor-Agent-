"""D4-VAL-001 定向测试：事件接收（来源、去重、冲突、序号、迟到、接收证据）。

R1 起：所有服务入口都需要受控入口注入的调用者上下文。
"""
import os
import tempfile
import types
import unittest
from pathlib import Path

from adapters.local_poc import LocalPocAdapter
from contracts.schemas import ContractError
from projections.read_models import ReadModels
from service.events import EventService
from service.reviews import ReviewService
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import (
    allowed_roots,
    create_task_payload,
    ctx_owner,
    ctx_reviewer,
    ctx_source,
    ctx_worker,
    runtime_root,
    validation_workspace,
)


def make_env(tmp, adapter=None):
    store = SqliteStore(Path(tmp) / "d4-events.sqlite3")
    store.register_source("qclaw-local", "本地验证来源", at="t0")
    store.register_source("local-poc", "本地 PoC 来源", at="t0")
    store.register_agent("dev-1", "QClaw 执行成员", "registry entry", at="t0")
    store.register_agent("dev-dwx1", "自报名称（非 dev-1 别名）", "self-reported", at="t0")
    tasks = TaskService(store, adapter=adapter or LocalPocAdapter(source_id="qclaw-local"),
                        allowed_roots=allowed_roots())
    env = types.SimpleNamespace(store=store, tasks=tasks)
    env.events = EventService(store)
    env.reviews = ReviewService(store, tasks)
    env.models = ReadModels(store)
    env.owner = ctx_owner()
    env.worker = ctx_worker()
    env.source = ctx_source()
    env.reviewer = ctx_reviewer()
    return env


def ev(event_id, seq, etype, task, attempt, body=None, **over):
    payload = {
        "schemaVersion": "event.v1",
        "eventId": event_id,
        "organizationId": "conductor",
        "projectId": task["projectId"],
        "taskId": task["taskId"],
        "attemptId": attempt["attemptId"],
        "sourceId": "qclaw-local",
        "agentId": "dev-1",
        "sequence": seq,
        "type": etype,
        "occurredAt": "2026-09-29T11:20:%02d+08:00" % (seq,),
        "payload": body or {},
    }
    payload.update(over)
    return payload


DELIVERY_BODY = {
    "summary": "完成验证文件和返回报告。",
    "changedFiles": [".conductor-local/d4-real-chain-validation/result.md"],
    "verification": [{"command": "node --check result.js", "result": "passed"}],
    "risks": ["尚未接入第二种执行环境"],
    "reportPath": os.path.join(validation_workspace(), "report.md"),
    "unfinished": [],
    "sourceDeliveryId": "qclaw-delivery-42-01",
}


class EventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.env = make_env(self.tmp.name)

    def tearDown(self):
        self.env.store.close()
        self.tmp.cleanup()

    def _task_and_attempt(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        return task, attempt

    def _ingest(self, payload, context=None):
        return self.env.events.ingest(payload, context=context or self.env.source)

    def test_submitted_without_host_confirmation_is_not_running(self):
        task, attempt = self._task_and_attempt()
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        detail = self.env.models.task_detail(task["taskId"])
        self.assertEqual(detail["displayState"], "submitted_pending_host")
        self.assertTrue(detail["currentAttempt"]["acceptanceMissing"])

    def test_normal_chain_records_milestones(self):
        task, attempt = self._task_and_attempt()
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        self._ingest(ev("evt-1", 1, "task.accepted", task, attempt, {"hostReceipt": "qclaw-ack-1"}))
        self.assertEqual(self.env.models.task_detail(task["taskId"])["displayState"], "accepted_by_host")
        self._ingest(ev("evt-2", 2, "task.started", task, attempt, {"note": "开始"}))
        self.assertEqual(self.env.models.task_detail(task["taskId"])["displayState"], "in_progress")
        self._ingest(ev("evt-3", 3, "task.progress", task, attempt,
                        {"step": "写入结果文件", "note": "步骤 1/2"}))
        delivered = self._ingest(ev("evt-4", 4, "task.delivered", task, attempt, DELIVERY_BODY))
        self.assertTrue(delivered["ok"])
        detail = self.env.models.task_detail(task["taskId"])
        self.assertEqual(detail["task"]["status"], "awaiting_review")
        self.assertEqual(detail["displayState"], "delivered")
        self.assertEqual(len(detail["deliveries"]), 1)
        self.assertEqual(detail["deliveries"][0]["report_path"],
                         os.path.join(validation_workspace(), "report.md"))
        self.assertEqual(detail["deliveries"][0]["unfinished_json"], [])
        queue = self.env.models.review_queue()
        self.assertEqual(queue["items"][0]["currentDeliveryId"], delivered["data"]["deliveryId"])

    def test_duplicate_event_same_content_is_replayed(self):
        task, attempt = self._task_and_attempt()
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        self._ingest(ev("evt-1", 1, "task.accepted", task, attempt, {"hostReceipt": "ack"}))
        first = self._ingest(ev("evt-2", 2, "task.delivered", task, attempt, DELIVERY_BODY))
        replay = self._ingest(ev("evt-2", 2, "task.delivered", task, attempt, DELIVERY_BODY))
        self.assertTrue(replay["replayed"])
        self.assertEqual(first["data"]["deliveryId"], replay["data"]["deliveryId"])
        self.assertEqual(self.env.store.count_deliveries(task["taskId"]), 1)

    def test_same_event_id_different_content_conflicts(self):
        task, attempt = self._task_and_attempt()
        self._ingest(ev("evt-1", 1, "task.progress", task, attempt, {"step": "a"}))
        with self.assertRaises(ContractError) as ctx:
            self._ingest(ev("evt-1", 2, "task.progress", task, attempt, {"step": "b"}))
        self.assertEqual(ctx.exception.code, "event_conflict")
        self.assertEqual(len(self.env.store.list_conflicts(task["taskId"])), 1)

    def test_stale_sequence_goes_to_history_without_regression(self):
        task, attempt = self._task_and_attempt()
        self._ingest(ev("evt-1", 1, "task.progress", task, attempt, {"step": "a"}))
        self._ingest(ev("evt-2", 2, "task.progress", task, attempt, {"step": "b"}))
        with self.assertRaises(ContractError) as ctx:
            self._ingest(ev("evt-3", 2, "task.progress", task, attempt, {"step": "old"}))
        self.assertEqual(ctx.exception.code, "stale_sequence")
        self.assertEqual(len(self.env.store.list_reconciliation(task["taskId"])), 1)

    def test_unknown_source_is_rejected(self):
        task, attempt = self._task_and_attempt()
        with self.assertRaises(ContractError) as ctx:
            self._ingest(ev("evt-1", 1, "task.started", task, attempt, sourceId="ghost-source"),
                         context=ctx_source("ghost-source"))
        self.assertEqual(ctx.exception.code, "unknown_source")

    def test_wrong_agent_is_rejected(self):
        task, attempt = self._task_and_attempt()
        with self.assertRaises(ContractError) as ctx:
            self._ingest(ev("evt-1", 1, "task.started", task, attempt, agentId="dev-dwx1"))
        self.assertEqual(ctx.exception.code, "permission_denied")

    def test_started_without_host_confirmation_stays_pending(self):
        task, attempt = self._task_and_attempt()
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        result = self._ingest(ev("evt-1", 1, "task.started", task, attempt, {"note": "无接收证据"}))
        self.assertTrue(result["data"].get("unverified"))
        detail = self.env.models.task_detail(task["taskId"])
        self.assertNotEqual(detail["displayState"], "in_progress")
        self.assertEqual(detail["displayState"], "submitted_pending_host")
        self.assertTrue(detail["currentAttempt"]["acceptanceMissing"])

    def test_accepted_without_evidence_is_rejected(self):
        task, attempt = self._task_and_attempt()
        with self.assertRaises(ContractError) as ctx:
            self._ingest(ev("evt-1", 1, "task.accepted", task, attempt, {}))
        self.assertEqual(ctx.exception.code, "host_unconfirmed")
        self.assertIsNone(self.env.store.get_attempt(attempt["attemptId"])["accepted_by_host_at"])

    def test_late_delivery_after_terminal_is_reconciled(self):
        task, attempt = self._task_and_attempt()
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        self._ingest(ev("evt-1", 1, "task.accepted", task, attempt, {"hostReceipt": "ack"}))
        self._ingest(ev("evt-2", 2, "task.delivered", task, attempt, DELIVERY_BODY))
        with self.assertRaises(ContractError) as ctx:
            self._ingest(ev("evt-3", 3, "task.delivered", task, attempt,
                            dict(DELIVERY_BODY, summary="终态后的交付")))
        # 执行轮次已结束：只入历史、不回退状态
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(self.env.store.count_deliveries(task["taskId"]), 1)
        self.assertEqual(len(self.env.store.list_reconciliation(task["taskId"])), 1)

    def test_blocked_does_not_auto_rerun(self):
        task, attempt = self._task_and_attempt()
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        self._ingest(ev("evt-1", 1, "task.blocked", task, attempt,
                        {"reason": "缺环境凭据", "owner": "cox", "resumeWhen": "提供凭据"}))
        detail = self.env.models.task_detail(task["taskId"])
        self.assertEqual(detail["displayState"], "blocked")
        self.assertEqual(detail["task"]["status"], "blocked")
        self.assertEqual(len(detail["deliveries"]), 0)

    def test_heartbeat_does_not_change_state(self):
        task, attempt = self._task_and_attempt()
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        before = self.env.store.get_task(task["taskId"])["status"]
        self._ingest(ev("evt-1", 1, "source.heartbeat", task, attempt,
                        {"sourceTime": "2026-09-29T11:20:00+08:00"}))
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], before)


if __name__ == "__main__":
    unittest.main()
