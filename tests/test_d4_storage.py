"""D4-VAL-001 定向测试：存储层（事务、唯一约束）。

仅新增测试；不修改或删除既有测试。
"""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path

from contracts.schemas import ContractError  # noqa: F401  (导入以确认契约包可加载)
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import (
    allowed_roots,
    create_task_payload,
    ctx_owner,
    runtime_root,
    validation_workspace,
)


def event_record(event_id="evt-1", **over):
    record = {
        "event_id": event_id, "task_id": "t1", "attempt_id": "a1", "project_id": "conductor",
        "source_id": "qclaw-local", "agent_id": "dev-1", "sequence": 1,
        "type": "task.started", "occurred_at": "2026-09-29T11:20:00+08:00",
        "received_at": "2026-09-29T11:20:01+08:00", "content_hash": "h1",
        "outcome": "applied", "reason": None, "result_json": "{}",
    }
    record.update(over)
    return record


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.db = Path(self.tmp.name) / "d4-store.sqlite3"
        self.store = SqliteStore(self.db)
        self.store.register_source("qclaw-local", "本地验证来源", at="t0")
        self.store.register_agent("dev-1", "QClaw 执行成员", "registry entry", at="t0")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_create_task_is_stable_and_persisted(self):
        service = TaskService(self.store, allowed_roots=allowed_roots())
        result = service.create_task(create_task_payload(), context=ctx_owner())
        task_id = result["taskId"]
        self.assertTrue(task_id.startswith("task-"))
        self.assertEqual(result["status"], "ready")
        task = self.store.get_task(task_id)
        self.assertEqual(task["agent_id"], "dev-1")
        self.assertEqual(task["project_id"], "conductor")
        self.assertEqual(task["workspace"], validation_workspace())
        self.assertIn("writeScope", task["permissions_json"])
        # 再次读取仍是同一 taskId（稳定）
        self.assertEqual(self.store.get_task(task_id)["task_id"], task_id)

    def test_event_id_unique_constraint(self):
        self.store.insert_event(event_record("evt-1"))
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.insert_event(event_record("evt-1", content_hash="h2"))

    def test_transaction_rolls_back_on_error(self):
        with self.assertRaises(RuntimeError):
            with self.store.transaction():
                self.store.register_source("s-keep", at="t")
                raise RuntimeError("boom")
        self.assertIsNone(self.store.get_source("s-keep"))

    def test_nested_transaction_uses_savepoint(self):
        with self.store.transaction():
            self.store.register_source("outer", at="t")
            with self.store.transaction():
                self.store.register_source("inner", at="t")
        self.assertIsNotNone(self.store.get_source("outer"))
        self.assertIsNotNone(self.store.get_source("inner"))

        # 内层失败只回滚内层，外层保留
        with self.store.transaction():
            self.store.register_source("outer2", at="t")
            try:
                with self.store.transaction():
                    self.store.register_source("inner2", at="t")
                    raise RuntimeError("inner boom")
            except RuntimeError:
                pass
        self.assertIsNotNone(self.store.get_source("outer2"))
        self.assertIsNone(self.store.get_source("inner2"))


if __name__ == "__main__":
    unittest.main()
