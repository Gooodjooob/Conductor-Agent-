import json

from scripts.d4_collect import append_audit, collect_once
from storage.sqlite_store import SqliteStore
from test_d4_file_receipts import FileReceiptFixture


class CollectorTests(FileReceiptFixture):
    def fixture_real_submission(self):
        # Test fixture only: exercise the collector's real-submission branch.
        store = SqliteStore(self.db)
        try:
            for row in store.list_activity(self.task_id):
                if row["kind"] == "attempt.submitted":
                    detail = json.loads(row["detail_json"])
                    detail["synthetic"] = False
                    store.conn.execute("UPDATE activity SET detail_json=? WHERE activity_id=?",
                                       (json.dumps(detail), row["activity_id"]))
        finally:
            store.close()

    def imports(self):
        store = SqliteStore(self.db, read_only=True)
        try:
            return [row for row in store.list_activity(self.task_id)
                    if row["kind"] == "file_receipt.imported"]
        finally:
            store.close()

    def test_missing_receipt_and_synthetic_submission(self):
        self.assertEqual(collect_once(self.db)[0]["reason"], "submission_not_real")
        self.fixture_real_submission()
        self.assertEqual(collect_once(self.db)[0]["status"], "waiting")
        self.assertEqual(self.imports(), [])

    def test_partial_receipt_then_delivery_without_duplicate_activity(self):
        self.fixture_real_submission()
        self.file.write_text(json.dumps({**self.bundle, "events": self.bundle["events"][:1]}))
        self.assertEqual(collect_once(self.db)[0]["status"], "imported")
        self.assertEqual(collect_once(self.db)[0]["status"], "unchanged")
        self.assertEqual(len(self.imports()), 1)
        self.file.write_text(json.dumps(self.bundle))
        self.assertEqual(collect_once(self.db)[0]["status"], "imported")
        self.assertEqual(collect_once(self.db)[0]["status"], "unchanged")
        self.assertEqual(len(self.imports()), 2)
        store = SqliteStore(self.db, read_only=True)
        try:
            self.assertEqual(store.get_task(self.task_id)["status"], "awaiting_review")
        finally:
            store.close()

    def test_conflicting_event_and_changed_file_not_silently_accepted(self):
        self.fixture_real_submission()
        self.file.write_text(json.dumps(self.bundle))
        self.assertEqual(collect_once(self.db)[0]["status"], "imported")
        self.bundle["events"][0]["occurredAt"] = "2026-10-01T01:00:00+08:00"
        self.file.write_text(json.dumps(self.bundle))
        self.assertEqual(collect_once(self.db)[0]["reason"], "event_conflict")
        self.assertEqual(len(self.imports()), 1)
        (self.folder / "result.md").write_text("changed")
        self.assertEqual(collect_once(self.db)[0]["reason"], "event_conflict")

    def test_no_host_confirmation_skipped_without_import(self):
        self.fixture_real_submission()
        store = SqliteStore(self.db)
        store.update_attempt(self.attempt_id, accepted_by_host_at=None)
        store.close()
        self.file.write_text(json.dumps(self.bundle))
        self.assertEqual(collect_once(self.db)[0]["reason"], "host_unconfirmed")
        self.assertEqual(self.imports(), [])

    def test_audit_is_private_and_rejects_symlink(self):
        target = self.root / "audit.jsonl"
        append_audit(target, [{"status": "waiting"}])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        link = self.root / "link.jsonl"
        link.symlink_to(target)
        with self.assertRaises(OSError):
            append_audit(link, [])

    def test_dsh_sdk_source_not_skipped_as_unknown(self):
        """D4-DSH-02-SDK-001：dsh-sdk 在受控来源白名单内。

        dsh-sdk 来源的已确认轮次不再因 unknown_source 跳过；
        其余校验（host-confirmation、synthetic 拒绝）与既有来源同口径。
        """
        from scripts.d4_collect import SOURCES
        from adapters.dsh_sdk import DSH_SDK_SOURCE_ID
        self.assertIn(DSH_SDK_SOURCE_ID, SOURCES)
        # 既有来源只增不删
        self.assertIn("qclaw-gateway", SOURCES)
        from adapters.workbuddy import WORKBUDDY_SOURCE_ID
        self.assertIn(WORKBUDDY_SOURCE_ID, SOURCES)

    def test_dsh_sdk_synthetic_submission_still_rejected(self):
        """synthetic 拒绝不因新来源削弱：dsh-sdk 同样规则（卡片 §5.4）。"""
        # 未 fixture_real_submission：synthetic → submission_not_real
        result = collect_once(self.db)[0]
        self.assertEqual(result["reason"], "submission_not_real")
