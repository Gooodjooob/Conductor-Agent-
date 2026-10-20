"""D4-VAL-001 R2 回归：并发、接收与目录边界的“预期拒绝或对账恢复”。

逐条覆盖 cox review.md 的七条探针，并覆盖路径正/负例与“重开库后可查询性”。
所有断言都要求 **fail closed**：不放宽 R1 的安全断言，只显式注入隔离根与授权上下文。
"""
import os
import tempfile
import unittest
from pathlib import Path

from contracts.paths import resolve_workspace
from contracts.schemas import ContractError
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
from test_d4_events import DELIVERY_BODY, ev, make_env


def _perms(**over):
    perms = {
        "writeScope": [".conductor-local/d4-real-chain-validation/"],
        "allowNetwork": False,
        "allowBusinessWrite": False,
        "allowGit": False,
        "allowPublish": False,
    }
    perms.update(over)
    return perms


class ProbeRegressionTests(unittest.TestCase):
    """七条探针：每条断言“预期拒绝”或“对账恢复”。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.env = make_env(self.tmp.name)

    def tearDown(self):
        self.env.store.close()
        self.tmp.cleanup()

    def _task_attempt(self, submit=True):
        task = self.env.tasks.create_task(create_task_payload(), context=self.env.owner)
        att = self.env.tasks.create_attempt(task["taskId"], context=self.env.worker)
        if submit:
            self.env.tasks.submit_attempt(task["taskId"], att["attemptId"], context=self.env.worker)
        return task, att

    # 探针 1：未提交 started 不能生效
    def test_probe_unsubmitted_started_not_accepted(self):
        task, att = self._task_attempt(submit=False)
        self.env.events.ingest(ev("accept", 1, "task.accepted", task, att, {"hostReceipt": "ack-1"}),
                               context=self.env.source)
        self.env.events.ingest(ev("start", 2, "task.started", task, att), context=self.env.source)
        row = self.env.store.get_attempt(att["attemptId"])
        self.assertIsNone(row["submitted_at"])
        self.assertIsNone(row["accepted_by_host_at"])
        self.assertNotEqual(self.env.models.task_detail(task["taskId"])["displayState"], "in_progress")

    # 探针 2：空白回执被拒
    def test_probe_whitespace_receipt_rejected(self):
        task, att = self._task_attempt()
        with self.assertRaises(ContractError) as ctx:
            self.env.events.ingest(
                ev("blank", 1, "task.accepted", task, att, {"hostReceipt": "   "}),
                context=self.env.source)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")
        self.assertIsNone(self.env.store.get_attempt(att["attemptId"])["accepted_by_host_at"])

    # 探针 3：拒绝的 delivered 首次结果持久化 + 同内容重发复现
    def test_probe_rejected_delivery_replay_is_persisted(self):
        task, att = self._task_attempt()
        payload = ev("early", 2, "task.delivered", task, att, dict(DELIVERY_BODY))
        with self.assertRaises(ContractError) as c1:
            self.env.events.ingest(payload, context=self.env.source)
        self.assertEqual(c1.exception.code, "invalid_transition")
        stored = self.env.store.get_event("early")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["outcome"], "rejected")
        # 接收确认后再重发同内容：返回首次拒绝结果，不创建交付
        self.env.events.ingest(ev("accept", 1, "task.accepted", task, att, {"hostReceipt": "ack-1"}),
                               context=self.env.source)
        with self.assertRaises(ContractError) as c2:
            self.env.events.ingest(payload, context=self.env.source)
        self.assertEqual(c2.exception.code, "invalid_transition")
        self.assertEqual(self.env.store.count_deliveries(task["taskId"]), 0)

    # 探针 4：任意 workspace 被服务允许根拒绝（声明层）
    def test_probe_arbitrary_workspace_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            self.env.tasks.create_task(
                create_task_payload(workspace="/Users/demo", permissions=_perms(writeScope=["."])),
                context=self.env.owner)
        self.assertEqual(ctx.exception.code, "path_escape")

    # 探针 5：未确认 progress 不挤掉合法接收里程碑
    def test_probe_late_accept_after_unconfirmed_progress(self):
        task, att = self._task_attempt()
        self.env.events.ingest(ev("progress", 2, "task.progress", task, att, {"step": "x"}),
                               context=self.env.source)
        result = self.env.events.ingest(
            ev("accept", 1, "task.accepted", task, att, {"hostReceipt": "ack-1"}),
            context=self.env.source)
        self.assertTrue(result["ok"])
        self.assertIsNotNone(self.env.store.get_attempt(att["attemptId"])["accepted_by_host_at"])
        self.assertEqual(self.env.models.task_detail(task["taskId"])["displayState"], "accepted_by_host")

    # 探针 6：旧 review 不能覆盖新返工（被拒）
    def _delivered(self):
        task, att = self._task_attempt()
        self.env.events.ingest(ev("accept", 1, "task.accepted", task, att, {"hostReceipt": "ack-1"}),
                               context=self.env.source)
        self.env.events.ingest(ev("delivery", 2, "task.delivered", task, att, dict(DELIVERY_BODY)),
                               context=self.env.source)
        delivery = self.env.store.get_task(task["taskId"])["current_delivery_id"]
        return task, att, delivery

    def test_probe_stale_review_does_not_override(self):
        task, _att, delivery = self._delivered()
        self.env.reviews.submit_review(
            task["taskId"], {"decision": "changes_requested", "deliveryId": delivery, "note": "返工"},
            context=self.env.reviewer)
        with self.assertRaises(ContractError) as ctx:
            self.env.reviews.submit_review(
                task["taskId"], {"decision": "approved", "deliveryId": delivery},
                context=self.env.reviewer)
        self.assertEqual(ctx.exception.code, "review_version_mismatch")
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], "active")

    # 探针 7：终态后迟到状态事件被拒（不回退 approved）
    def test_probe_late_blocked_after_approved(self):
        task, att, delivery = self._delivered()
        self.env.reviews.submit_review(
            task["taskId"], {"decision": "approved", "deliveryId": delivery},
            context=self.env.reviewer)
        with self.assertRaises(ContractError) as ctx:
            self.env.events.ingest(
                ev("late-block", 4, "task.blocked", task, att,
                   {"reason": "synthetic", "owner": "cox", "resumeWhen": "test"}),
                context=self.env.source)
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(self.env.store.get_task(task["taskId"])["status"], "approved")


class AllowedRootTests(unittest.TestCase):
    """目录上限由服务配置注入：正例 + 父目录/任意目录/符号链接负例。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_workspace_within_allowed_root_ok(self):
        ws = str(self.root / "ws")
        os.makedirs(ws)
        self.assertEqual(resolve_workspace(ws, [str(self.root)]), os.path.realpath(ws))

    def test_missing_allowed_roots_fail_closed(self):
        with self.assertRaises(ContractError) as ctx:
            resolve_workspace(str(self.root), None)
        self.assertEqual(ctx.exception.code, "permission_denied")

    def test_arbitrary_root_is_rejected(self):
        with self.assertRaises(ContractError) as ctx:
            resolve_workspace("/Users/demo", [str(self.root)])
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_parent_of_allowed_root_is_rejected(self):
        inner = self.root / "inner"
        inner.mkdir()
        with self.assertRaises(ContractError) as ctx:
            resolve_workspace(str(self.root), [str(inner)])
        self.assertEqual(ctx.exception.code, "path_escape")

    def test_symlink_workspace_escape_rejected(self):
        allowed = self.root / "allowed"
        outside = self.root / "outside"
        allowed.mkdir()
        outside.mkdir()
        (allowed / "escape").symlink_to(outside, target_is_directory=True)
        # 符号链接目标落在 allowed 之外 -> realpath 后越界
        with self.assertRaises(ContractError) as ctx:
            resolve_workspace(str(allowed / "escape"), [str(allowed)])
        self.assertEqual(ctx.exception.code, "path_escape")


class PersistenceAfterReopenTests(unittest.TestCase):
    """重开 SQLite 后：首次拒绝结果、reviewer 身份、来源、模拟标记、交付历史与编号仍可查。"""

    def test_rejections_and_reviews_survive_reopen(self):
        tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        try:
            env = make_env(tmp.name)
            task = env.tasks.create_task(create_task_payload(), context=env.owner)
            att = env.tasks.create_attempt(task["taskId"], context=env.worker)
            env.tasks.submit_attempt(task["taskId"], att["attemptId"], context=env.worker)
            with self.assertRaises(ContractError):
                env.events.ingest(ev("early", 2, "task.delivered", task, att, dict(DELIVERY_BODY)),
                                  context=env.source)
            env.events.ingest(ev("accept", 1, "task.accepted", task, att, {"hostReceipt": "ack-1"}),
                              context=env.source)
            env.events.ingest(ev("delivery", 2, "task.delivered", task, att, dict(DELIVERY_BODY)),
                              context=env.source)
            delivery = env.store.get_task(task["taskId"])["current_delivery_id"]
            env.reviews.submit_review(
                task["taskId"], {"decision": "changes_requested", "deliveryId": delivery, "note": "返工"},
                context=env.reviewer)
            db_path = env.store.path
            tid = task["taskId"]
            env.store.close()

            reopened = SqliteStore(db_path)
            try:
                rejected = reopened.get_event("early")
                self.assertIsNotNone(rejected)
                self.assertEqual(rejected["outcome"], "rejected")
                self.assertEqual(rejected["outcome_code"], "invalid_transition")
                self.assertEqual(rejected["source_id"], "qclaw-local")
                self.assertEqual(rejected["synthetic"], 1)
                reviews = reopened.list_reviews(tid)
                self.assertEqual(reviews[0]["decision"], "changes_requested")
                self.assertEqual(reviews[0]["reviewer_id"], "cox")
                self.assertEqual(reviews[0]["review_round"], 1)
                self.assertEqual(len(reopened.list_deliveries(tid)), 1)
            finally:
                reopened.close()
        finally:
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
