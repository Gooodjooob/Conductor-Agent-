"""D4-VAL-001 定向测试：适配器能力报告与 fail-closed 边界。

关键断言：能力未验证的宿主绝不被报告为“已接通”。
"""
import tempfile
import unittest
from pathlib import Path

from adapters.base import HostAdapter
from adapters.local_poc import LocalPocAdapter, UnverifiedHostAdapter
from contracts.schemas import ContractError
from service.tasks import TaskService
from storage.sqlite_store import SqliteStore

from test_d4_contracts import (
    allowed_roots,
    create_task_payload,
    ctx_owner,
    ctx_worker,
    runtime_root,
)


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.db = Path(self.tmp.name) / "d4-adapter.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def test_local_poc_reports_limited_capability(self):
        adapter = LocalPocAdapter()
        report = adapter.capability_report()
        self.assertFalse(report["connected"])
        self.assertTrue(report["capabilities"]["submit"])
        self.assertFalse(report["capabilities"]["host_acceptance"])
        self.assertEqual(report["capabilities"]["evidence_level"], "LOCAL")

    def test_local_poc_submit_is_submitted_only(self):
        adapter = LocalPocAdapter()
        result = adapter.submit(task=None, attempt=None)
        self.assertEqual(result.status, "submitted")
        self.assertIsNone(result.host_receipt)

    def test_unverified_host_is_fail_closed(self):
        adapter = UnverifiedHostAdapter("qclaw-local")
        report = adapter.capability_report()
        self.assertFalse(report["connected"])
        self.assertFalse(report["capabilities"]["host_acceptance"])
        self.assertEqual(report["capabilities"]["evidence_level"], "UNVERIFIED")
        with self.assertRaises(ContractError) as ctx:
            adapter.submit(task=None, attempt=None)
        self.assertEqual(ctx.exception.code, "host_unconfirmed")

    def test_service_does_not_record_submission_when_host_unverified(self):
        store = SqliteStore(self.db)
        store.register_source("qclaw-local", "本地验证来源", at="t0")
        store.register_agent("dev-1", "QClaw 执行成员", "registry", at="t0")
        service = TaskService(store, adapter=UnverifiedHostAdapter("qclaw-local"),
                              allowed_roots=allowed_roots())
        task = service.create_task(create_task_payload(), context=ctx_owner())
        attempt = service.create_attempt(task["taskId"], context=ctx_worker())
        with self.assertRaises(ContractError) as ctx:
            service.submit_attempt(task["taskId"], attempt["attemptId"], context=ctx_worker())
        self.assertEqual(ctx.exception.code, "host_unconfirmed")
        # fail closed：没有写入任何 submitted 证据
        self.assertIsNone(store.get_attempt(attempt["attemptId"])["submitted_at"])
        store.close()

    def test_abstract_adapter_requires_implementation(self):
        with self.assertRaises(NotImplementedError):
            HostAdapter().capabilities()
        with self.assertRaises(NotImplementedError):
            HostAdapter().submit(None, None)


if __name__ == "__main__":
    unittest.main()
