"""D4-VAL-001 定向测试：cox 审核决定与返工执行轮次（R1：注入审核者上下文）。"""
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
)
from test_d4_events import DELIVERY_BODY, ev


def make_env(tmp, adapter=None):
    store = SqliteStore(Path(tmp) / "d4-reviews.sqlite3")
    store.register_source("qclaw-local", "本地验证来源", at="t0")
    store.register_agent("dev-1", "QClaw 执行成员", "registry entry", at="t0")
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


class ReviewTests(unittest.TestCase):
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

    def test_approved_only_closes_review(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery_id = self._drive_to_delivery(task, attempt)
        result = self.env.reviews.submit_review(task["taskId"], {
            "decision": "approved", "deliveryId": delivery_id, "note": "范围和证据符合。",
        }, context=self.env.reviewer)
        self.assertEqual(result["taskStatus"], "approved")
        self.assertEqual(result["closure"],
                         {"qaExecuted": False, "userConfirmed": False, "git": False, "published": False})
        task_row = self.env.store.get_task(task["taskId"])
        self.assertEqual(task_row["status"], "approved")
        # 通过只改变审核/任务状态，不写成 QA、Git 或发布完成
        for wrong in ("qa_passed", "user_confirmed", "git", "published", "released"):
            self.assertNotEqual(task_row["status"], wrong)

    def test_changes_requested_creates_new_attempt_and_keeps_old_delivery(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt1 = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery1 = self._drive_to_delivery(task, attempt1)
        result = self.env.reviews.submit_review(task["taskId"], {
            "decision": "changes_requested", "deliveryId": delivery1,
            "note": "请在报告里补上环境收到时间和失败处理说明。",
        }, context=self.env.reviewer)
        self.assertEqual(self.env.store.count_attempts(task["taskId"]), 2)
        self.assertNotEqual(result["resultingAttemptId"], attempt1["attemptId"])
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], "active")
        # 旧交付仍可查询
        old = self.env.store.get_delivery(delivery1)
        self.assertIsNotNone(old)
        self.assertEqual(old["delivery_id"], delivery1)
        review = self.env.store.list_reviews(task["taskId"])[-1]
        self.assertEqual(review["resulting_attempt_id"], result["resultingAttemptId"])
        self.assertEqual(review["reviewer_id"], "cox")

    def test_review_of_old_delivery_is_rejected(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt1 = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery1 = self._drive_to_delivery(task, attempt1, prefix="a")
        self.env.reviews.submit_review(task["taskId"], {
            "decision": "changes_requested", "deliveryId": delivery1, "note": "返工",
        }, context=self.env.reviewer)
        attempt2 = {"attemptId": self.env.store.get_task(task["taskId"])["current_attempt_id"]}
        self._drive_to_delivery(task, attempt2, prefix="b")
        with self.assertRaises(ContractError) as ctx:
            self.env.reviews.submit_review(task["taskId"], {
                "decision": "approved", "deliveryId": delivery1,
            }, context=self.env.reviewer)
        self.assertEqual(ctx.exception.code, "review_version_mismatch")

    def test_rework_beyond_max_attempts_is_blocked(self):
        task = self.env.tasks.create_task(
            create_task_payload(limits={"maxAttempts": 1}), context=self.env.owner
        )
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery = self._drive_to_delivery(task, attempt)
        with self.assertRaises(ContractError) as ctx:
            self.env.reviews.submit_review(task["taskId"], {
                "decision": "changes_requested", "deliveryId": delivery, "note": "再改一轮",
            }, context=self.env.reviewer)
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(self.env.store.count_attempts(task["taskId"]), 1)

    def test_blocked_and_request_info(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery = self._drive_to_delivery(task, attempt)
        blocked = self.env.reviews.submit_review(task["taskId"], {
            "decision": "blocked", "deliveryId": delivery, "note": "等待 qclaw-local 凭据",
        }, context=self.env.reviewer)
        self.assertEqual(blocked["taskStatus"], "blocked")
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], "blocked")

        task2 = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt2 = self.env.tasks.create_attempt(task2["taskId"], context=self.env.worker)
        delivery2 = self._drive_to_delivery(task2, attempt2, prefix="c")
        info = self.env.reviews.submit_review(task2["taskId"], {
            "decision": "request_info", "deliveryId": delivery2, "note": "补充验收口径",
        }, context=self.env.reviewer)
        self.assertEqual(info["taskStatus"], "awaiting_review")
        self.assertEqual(self.env.store.count_attempts(task2["taskId"]), 1)

    def test_invalid_decision_is_rejected(self):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        attempt = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        delivery = self._drive_to_delivery(task, attempt)
        with self.assertRaises(ContractError) as ctx:
            self.env.reviews.submit_review(task["taskId"], {
                "decision": "looks_fine", "deliveryId": delivery,
            }, context=self.env.reviewer)
        self.assertEqual(ctx.exception.code, "invalid_review")


if __name__ == "__main__":
    unittest.main()
