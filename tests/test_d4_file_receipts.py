import hashlib
import json
from pathlib import Path
from unittest import mock

from service import file_receipts
from service.file_receipts import MAX_BYTES, read_regular, receipt_directory
from contracts.schemas import ContractError
from test_d4ctl import D4CtlTestCase, ResponsiveTransport, event_payload


class FileReceiptFixture(D4CtlTestCase):
    def setUp(self):
        super().setUp()
        self.workspace = str(self.root)
        code, result = self.create_task(workspace=self.workspace,
                                       permissions={"writeScope": ["."], "allowNetwork": False,
                                                    "allowBusinessWrite": False, "allowGit": False,
                                                    "allowPublish": False})
        self.assertEqual(code, 0, result)
        self.task_id = result["data"]["taskId"]
        submitted = self.submit_first_attempt(self.task_id, ResponsiveTransport())
        self.attempt_id = submitted["attemptId"]
        self.folder = self.root / receipt_directory(self.task_id, self.attempt_id)
        self.folder.mkdir(parents=True)
        for name in ("result.md", "delivery-report.md"):
            (self.folder / name).write_text("actual fixture")
        self.file = self.folder / "events.json"
        relative = str(self.folder.relative_to(self.root))
        self.bundle = {"hostRunId": self.attempt_id, "events": [
            event_payload(self.task_id, self.attempt_id, "task.started", 1),
            event_payload(self.task_id, self.attempt_id, "task.delivered", 2,
                          payload={"summary": "fixture", "reportPath": relative + "/delivery-report.md",
                                   "changedFiles": [relative + "/result.md"],
                                   "verification": ["fixture read"], "risks": [], "unfinished": []}),
        ]}

    def import_file(self):
        self.file.write_text(json.dumps(self.bundle))
        return self.cli(["event", "import", "--task", self.task_id,
                         "--attempt", self.attempt_id, "--file", str(self.file)])


class FileReceiptTests(FileReceiptFixture):

    def test_real_files_import_and_identical_replay(self):
        code, result = self.import_file()
        self.assertEqual(code, 0, result)
        self.assertEqual(len(result["data"]["fileSha256"]), 2)
        self.assertTrue(result["data"]["synthetic"])
        code, replay = self.import_file()
        self.assertEqual(code, 0, replay)

    def test_run_binding_mismatch(self):
        self.bundle["hostRunId"] = "another-run"
        self.assertEqual(self.import_file()[1]["error"]["code"], "host_unconfirmed")

    def test_changed_artifact_after_delivery_is_rejected(self):
        self.assertEqual(self.import_file()[0], 0)
        (self.folder / "result.md").write_text("changed")
        self.assertEqual(self.import_file()[1]["error"]["code"], "event_conflict")

    def test_import_cannot_promote_synthetic_submission_to_real(self):
        self.file.write_text(json.dumps(self.bundle))
        code, result = self.cli(["event", "import", "--task", self.task_id,
                                "--attempt", self.attempt_id, "--file", str(self.file)], synthetic=False)
        self.assertEqual(code, 0, result)
        self.assertTrue(result["data"]["synthetic"])

    def test_event_binding_mismatch_before_any_import(self):
        self.bundle["events"][1]["attemptId"] = "other"
        self.assertEqual(self.import_file()[1]["error"]["code"], "permission_denied")

    def test_other_round_report_rejected(self):
        self.bundle["events"][1]["payload"]["reportPath"] = "report.md"
        self.assertEqual(self.import_file()[1]["error"]["code"], "path_escape")

    def test_symlink_report_rejected(self):
        (self.folder / "delivery-report.md").unlink()
        (self.folder / "delivery-report.md").symlink_to(self.folder / "result.md")
        self.assertEqual(self.import_file()[1]["error"]["code"], "path_escape")

    def test_identity_and_privileged_event_rejected(self):
        for key, value in (("sourceId", "other"), ("agentId", "cox"), ("type", "task.accepted")):
            old = self.bundle["events"][0][key]
            self.bundle["events"][0][key] = value
            self.assertEqual(self.import_file()[1]["error"]["code"], "permission_denied")
            self.bundle["events"][0][key] = old

    def test_missing_report_rejected(self):
        (self.folder / "delivery-report.md").unlink()
        self.assertEqual(self.import_file()[1]["error"]["code"], "path_escape")

    def test_unsafe_file_shapes(self):
        oversized = self.folder / "large"
        oversized.write_bytes(b"x" * (MAX_BYTES + 1))
        for path in (self.folder, oversized):
            with self.assertRaises(ContractError):
                read_regular(self.workspace, str(path))


class ScopedFileReceiptFixture(D4CtlTestCase):
    """writeScope 只放开 src/ 与 receipts/ 的脚手架。

    用于验证返工卡的交付边界：报告文件仍在回执目录，源码 changedFiles 允许 writeScope
    内的授权路径。合成数据，不接触真实宿主或业务接口。
    """

    def setUp(self):
        super().setUp()
        self.workspace = str(self.root)
        code, result = self.create_task(workspace=self.workspace,
                                        permissions={"writeScope": ["src", "receipts"],
                                                     "allowNetwork": False,
                                                     "allowBusinessWrite": False,
                                                     "allowGit": False,
                                                     "allowPublish": False})
        self.assertEqual(code, 0, result)
        self.task_id = result["data"]["taskId"]
        submitted = self.submit_first_attempt(self.task_id, ResponsiveTransport())
        self.attempt_id = submitted["attemptId"]
        self.folder = self.root / receipt_directory(self.task_id, self.attempt_id)
        self.folder.mkdir(parents=True)
        for name in ("result.md", "delivery-report.md"):
            (self.folder / name).write_text("actual fixture")
        self.receipts = str(self.folder.relative_to(self.root))
        (self.root / "src").mkdir()
        self.app = self.root / "src" / "app.py"
        self.app.write_text("print('source')\n")
        (self.root / "outside").mkdir()
        (self.root / "outside" / "secret.txt").write_text("secret")
        self.file = self.folder / "events.json"

    def bundle(self, changed_files, report_path=None):
        return {"hostRunId": self.attempt_id, "events": [
            event_payload(self.task_id, self.attempt_id, "task.started", 1),
            event_payload(self.task_id, self.attempt_id, "task.delivered", 2,
                          payload={"summary": "fixture",
                                   "reportPath": report_path or self.receipts + "/delivery-report.md",
                                   "changedFiles": changed_files,
                                   "verification": ["fixture read"], "risks": [], "unfinished": []}),
        ]}

    def import_bundle(self, bundle):
        self.file.write_text(json.dumps(bundle))
        return self.cli(["event", "import", "--task", self.task_id,
                         "--attempt", self.attempt_id, "--file", str(self.file)])


class FileReceiptAuthorizedSourceTests(ScopedFileReceiptFixture):
    """返工卡正例/负例：授权源码路径可导入，越界形态继续拒绝。"""

    def test_authorized_source_file_imports_with_receipt_report(self):
        code, result = self.import_bundle(self.bundle(["src/app.py"]))
        self.assertEqual(code, 0, result)
        digests = result["data"]["fileSha256"]
        self.assertEqual(len(digests), 2)
        self.assertEqual(digests[str(self.app)],
                         hashlib.sha256(self.app.read_bytes()).hexdigest())
        self.assertEqual(digests[str(self.folder / "delivery-report.md")],
                         hashlib.sha256(b"actual fixture").hexdigest())

    def test_source_file_outside_write_scope_rejected(self):
        code, result = self.import_bundle(self.bundle(["outside/secret.txt"]))
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "path_escape")

    def test_source_path_outside_workspace_rejected(self):
        for entry in ("../escape.py", str(self.root.parent / "escape.py"), "/etc/passwd"):
            code, result = self.import_bundle(self.bundle([entry]))
            self.assertEqual(result["error"]["code"], "path_escape", entry)
            self.assertEqual(code, 1, entry)

    def test_symlink_source_file_rejected(self):
        escaping = self.root / "src" / "outside-link.py"
        escaping.symlink_to(self.root / "outside" / "secret.txt")
        inside = self.root / "src" / "alias.py"
        inside.symlink_to(self.app)
        for entry in ("src/outside-link.py", "src/alias.py"):
            self.assertEqual(self.import_bundle(self.bundle([entry]))[1]["error"]["code"],
                             "path_escape", entry)

    def test_directory_and_oversized_source_rejected(self):
        oversized = self.root / "src" / "big.py"
        oversized.write_bytes(b"x" * (MAX_BYTES + 1))
        # 目录与超大文件都先通过 writeScope 声明校验，再被 read_regular 的普通文件/大小检查拒绝。
        for entry in ("src", "src/big.py"):
            code, result = self.import_bundle(self.bundle([entry]))
            self.assertEqual(code, 1, entry)
            self.assertEqual(result["error"]["code"], "invalid_event", entry)

    def test_report_path_must_stay_in_current_receipt_directory(self):
        other = self.root / "receipts" / "att-other"
        other.mkdir(parents=True)
        (other / "delivery-report.md").write_text("other round")
        for entry in ("src/app.py", "receipts/att-other/delivery-report.md"):
            code, result = self.import_bundle(self.bundle(["src/app.py"], report_path=entry))
            self.assertEqual(result["error"]["code"], "path_escape", entry)
            self.assertEqual(code, 1, entry)

    def test_report_named_changed_file_outside_receipt_directory_rejected(self):
        (self.root / "src" / "result.md").write_text("looks like a report")
        code, result = self.import_bundle(self.bundle(["src/result.md"]))
        self.assertEqual(result["error"]["code"], "path_escape")
        self.assertEqual(code, 1)

    def test_rejected_bundle_leaves_no_partial_import(self):
        self.assertEqual(self.import_bundle(self.bundle(["outside/secret.txt"]))[1]
                         ["error"]["code"], "path_escape")
        code, result = self.import_bundle(self.bundle(["src/app.py"]))
        self.assertEqual(code, 0, result)
        # 被拒回执没有留下任何已导入记录：合法回执是首次导入（非重放）；
        # 同一文件再次导入仍按同一摘要重放。
        self.assertNotIn("replayed", result["data"])
        self.assertTrue(self.import_bundle(self.bundle(["src/app.py"]))[1]["data"]["replayed"])

    def test_duplicate_delivery_entry_is_read_once(self):
        report = self.receipts + "/delivery-report.md"
        calls = []
        original = file_receipts.read_regular

        def counting(workspace, entry):
            calls.append(str(entry))
            return original(workspace, entry)

        with mock.patch.object(file_receipts, "read_regular", counting):
            code, result = self.import_bundle(self.bundle([report, self.receipts + "/result.md"]))
        self.assertEqual(code, 0, result)
        digests = result["data"]["fileSha256"]
        self.assertEqual(len(digests), 2)
        self.assertEqual(digests[str(self.folder / "delivery-report.md")],
                         hashlib.sha256(b"actual fixture").hexdigest())
        self.assertEqual(sum(1 for call in calls if call.endswith("delivery-report.md")), 1)
        self.assertEqual(sum(1 for call in calls if call.endswith("events.json")), 1)

    def test_same_file_as_report_path_and_changed_file_read_once(self):
        """同一文件既作 reportPath（绝对写法）又作 changedFiles（相对写法）→ 只读一次、单一摘要。"""
        absolute = str(self.folder / "delivery-report.md")
        relative = self.receipts + "/delivery-report.md"
        calls = []
        original = file_receipts.read_regular

        def counting(workspace, entry):
            calls.append(str(entry))
            return original(workspace, entry)

        with mock.patch.object(file_receipts, "read_regular", counting):
            code, result = self.import_bundle(self.bundle([relative], report_path=absolute))
        self.assertEqual(code, 0, result)
        self.assertEqual(result["data"]["fileSha256"],
                         {absolute: hashlib.sha256(b"actual fixture").hexdigest()})
        self.assertEqual(sum(1 for call in calls if call.endswith("delivery-report.md")), 1)
