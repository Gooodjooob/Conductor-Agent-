"""D4-VAL-001 R1 回归测试：逐一复现 cox 首轮 review 的 9 个缺陷并锁定修复。

对应 R1 返工卡“cox 9 个复现测试名”。每条都断言“修好后的正确行为”，
不复用 cox 旧脚本签名，也不通过提前报错来假通过。
"""
import tempfile
import unittest

from adapters.base import AdapterResult
from adapters.local_poc import LocalPocAdapter
from contracts.schemas import ContractError
from service.events import EventService
from service.reviews import ReviewService
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import (
    create_task_payload,
    ctx_owner,
    ctx_reviewer,
    ctx_source,
    ctx_worker,
    runtime_root,
)
from test_d4_events import DELIVERY_BODY, ev, make_env


class CountingAdapter(LocalPocAdapter):
    """只统计被真实调用次数的本地 PoC 适配器。"""

    def __init__(self, source_id="qclaw-local", result=None):
        super().__init__(source_id=source_id)
        self.calls = 0
        self._result = result

    def submit(self, task, attempt):
        self.calls += 1
        if self._result is not None:
            return self._result
        return super().submit(task, attempt)


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.env = make_env(self.tmp.name)

    def tearDown(self):
        self.env.store.close()
        self.tmp.cleanup()

    def _drive_to_delivery(self, task, attempt, prefix="evt"):
        env = self.env
        env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=env.worker)
        env.events.ingest(
            ev("%s-1" % prefix, 1, "task.accepted", task, attempt, {"hostReceipt": "ack"}),
            context=env.source,
        )
        env.events.ingest(
            ev("%s-2" % prefix, 2, "task.started", task, attempt, {"note": "开始"}), context=env.source
        )
        result = env.events.ingest(
            ev("%s-3" % prefix, 3, "task.delivered", task, attempt, DELIVERY_BODY), context=env.source
        )
        return result["data"]["deliveryId"]

    # 1. 防重复提交：重复 submit 只调用一次适配器
    def test_duplicate_submit_calls_adapter_once(self):
        adapter = CountingAdapter(source_id="qclaw-local")
        env = make_env(self.tmp.name, adapter)
        task = env.tasks.create_task(create_task_payload(), context=env.owner)
        attempt = env.tasks.create_attempt(task["taskId"], context=env.worker)

        first = env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=env.worker)
        second = env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=env.worker)

        self.assertEqual(adapter.calls, 1)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["submission"], second["submission"])
        env.store.close()

    # 2. 缺审核者身份：默认拒绝
    def test_missing_reviewer_identity_rejected(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery = self._drive_to_delivery(task, attempt)
        with self.assertRaises(ContractError) as ctx:
            self.env.reviews.submit_review(
                task["taskId"], {"decision": "approved", "deliveryId": delivery}, context=None
            )
        self.assertEqual(ctx.exception.code, "permission_denied")
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], "awaiting_review")
        # 也不信任 payload 自报审核者
        with self.assertRaises(ContractError) as ctx2:
            self.env.reviews.submit_review(task["taskId"], {
                "decision": "approved", "deliveryId": delivery, "reviewerId": "cox", "role": "reviewer",
            }, context=ctx_worker())
        self.assertEqual(ctx2.exception.code, "permission_denied")

    # 3. 返工期间旧交付不再是 current
    def test_previous_delivery_not_current_during_rework(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt1 = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery1 = self._drive_to_delivery(task, attempt1)
        self.env.reviews.submit_review(task["taskId"], {
            "decision": "changes_requested", "deliveryId": delivery1, "note": "返工",
        }, context=self.env.reviewer)
        # 旧交付仍可查，但不再是 current
        self.assertIsNotNone(self.env.store.get_delivery(delivery1))
        self.assertIsNone(self.env.store.get_task(task["taskId"])["current_delivery_id"])
        with self.assertRaises(ContractError) as ctx:
            self.env.reviews.submit_review(task["taskId"], {
                "decision": "approved", "deliveryId": delivery1,
            }, context=self.env.reviewer)
        self.assertEqual(ctx.exception.code, "review_version_mismatch")

    # 4. 适配器返回已登记但错误的来源 -> 拒绝，不落提交证据
    def test_registered_wrong_source_rejected(self):
        bad = AdapterResult(status="submitted", source_id="local-poc", note="错来源", evidence_level="LOCAL")
        adapter = CountingAdapter(source_id="local-poc", result=bad)
        env = make_env(self.tmp.name, adapter)
        task = env.tasks.create_task(create_task_payload(), context=env.owner)
        attempt = env.tasks.create_attempt(task["taskId"], context=env.worker)
        with self.assertRaises(ContractError) as ctx:
            env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=env.worker)
        self.assertEqual(ctx.exception.code, "permission_denied")
        row = env.store.get_attempt(attempt["attemptId"])
        self.assertIsNone(row["submitted_at"])
        self.assertEqual(row["submit_state"], "unknown")
        env.store.close()

    # 5. writeScope 逃逸 -> 创建任务即拒绝
    def test_scope_escape_rejected_at_task_creation(self):
        escaping = create_task_payload(permissions={
            "writeScope": ["../../etc"],
            "allowNetwork": False,
            "allowBusinessWrite": False,
            "allowGit": False,
            "allowPublish": False,
        })
        with self.assertRaises(ContractError) as ctx:
            self.env.tasks.create_task(escaping, context=self.env.owner)
        self.assertEqual(ctx.exception.code, "path_escape")
        self.assertEqual(self.env.store.list_tasks(), [])

    # 6. 重复上报旧序号：第一次拒绝结果被保留（不变成成功）
    def test_stale_replay_preserves_rejection(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        self.env.events.ingest(
            ev("evt-1", 1, "task.progress", task, attempt, {"step": "a"}), context=self.env.source
        )
        self.env.events.ingest(
            ev("evt-2", 2, "task.progress", task, attempt, {"step": "b"}), context=self.env.source
        )
        stale = ev("evt-old", 2, "task.progress", task, attempt, {"step": "old"})
        with self.assertRaises(ContractError) as ctx:
            self.env.events.ingest(stale, context=self.env.source)
        self.assertEqual(ctx.exception.code, "stale_sequence")
        # 完全相同内容重放 -> 仍复现同一拒绝码，绝不假成功
        with self.assertRaises(ContractError) as ctx2:
            self.env.events.ingest(
                ev("evt-old", 2, "task.progress", task, attempt, {"step": "old"}),
                context=self.env.source,
            )
        self.assertEqual(ctx2.exception.code, "stale_sequence")
        self.assertEqual(self.env.store.get_event("evt-old")["outcome"], "stale")

    # 7. 终态（已通过）不能被 blocked 回退
    def test_terminal_cannot_be_regressed_by_blocked(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery = self._drive_to_delivery(task, attempt)
        self.env.reviews.submit_review(task["taskId"], {
            "decision": "approved", "deliveryId": delivery, "note": "OK",
        }, context=self.env.reviewer)
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], "approved")
        with self.assertRaises(ContractError) as ctx:
            self.env.events.ingest(
                ev("evt-late", 4, "task.blocked", task, attempt,
                   {"reason": "迟到阻塞", "owner": "cox", "resumeWhen": "x"}),
                context=self.env.source,
            )
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], "approved")
        self.assertEqual(len(self.env.store.list_reconciliation(task["taskId"])), 1)

    # 8. 未获接收确认的交付不能被通过
    def test_unconfirmed_delivery_cannot_be_approved(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        self.env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=self.env.worker)
        # 没有 task.accepted 事件，直接交付 -> 拒绝
        with self.assertRaises(ContractError) as ctx:
            self.env.events.ingest(
                ev("evt-1", 1, "task.delivered", task, attempt, DELIVERY_BODY), context=self.env.source
            )
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(self.env.store.count_deliveries(task["taskId"]), 0)
        self.assertNotEqual(self.env.store.get_task(task["taskId"])["status"], "awaiting_review")

    # 9. 未验证的“已接收”结果不被采信
    def test_unverified_acceptance_result_not_trusted(self):
        fake = AdapterResult(
            status="accepted_by_host", source_id="qclaw-local",
            host_receipt=None, note="伪造接收", evidence_level="UNVERIFIED",
        )
        adapter = CountingAdapter(source_id="qclaw-local", result=fake)
        env = make_env(self.tmp.name, adapter)
        task = env.tasks.create_task(create_task_payload(), context=env.owner)
        attempt = env.tasks.create_attempt(task["taskId"], context=env.worker)
        with self.assertRaises(ContractError) as ctx:
            env.tasks.submit_attempt(task["taskId"], attempt["attemptId"], context=env.worker)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")
        row = env.store.get_attempt(attempt["attemptId"])
        self.assertIsNone(row["accepted_by_host_at"])
        self.assertIsNone(row["submitted_at"])
        self.assertEqual(row["submit_state"], "unknown")
        env.store.close()


if __name__ == "__main__":
    unittest.main()
