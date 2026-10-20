"""D4-PROJECT-01 定向测试：项目资料的只读预检（service + d4ctl project inspect）。

覆盖任务卡验收的预检面：
- 结构检查不依赖来源文件存在；来源检查单独标明是否可应用；
- 占位符必须返回待补充，不能算可应用；
- 越界 / 符号链接 / 凭据目录 / 整目录 / 超限来源都有确定结论；
- 预检前后正式数据库与来源文件不变，不存在的库不会被创建；
- 合法预检结果可对拍（同一输入两次得到同一来源快照）；
- d4ctl 既有子命令行为不变。

全部输入为临时目录内的合成材料；不触真实 CRM / ERP / 客户数据。
"""
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

from contracts.document_io import DocumentContractError
from contracts.project_intake import ProjectIntakeContractError
from scripts import d4ctl
from service.project_intake import (
    compare_material_snapshots,
    material_digest,
    preflight_document,
    snapshot_materials,
)
from test_d4_project_intake_contract import IDENTITY, REPORT_SUMMARY, TASK_SUMMARY


class PreflightCase(unittest.TestCase):
    """公共夹具：一个受控允许根 + 合成的产品/卡/报告来源。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.sources = self.root / "sources"
        self.sources.mkdir()
        self.product = self.sources / "product.md"
        self.product.write_text("# 项目说明\n\n合成来源，不涉及真实业务数据。\n", encoding="utf-8")
        self.card = self.sources / "task-card.md"
        self.card.write_text("# 任务卡\n\n合成任务卡。\n", encoding="utf-8")
        self.report = self.sources / "progress-report.md"
        self.report.write_text("# 进展报告\n\n合成进展。\n", encoding="utf-8")
        self.db = self.root / "d4.db"

    def intake_object(self, **over):
        project = {
            "projectId": "demo-docs",
            "name": "演示资料整理项目",
            "goal": "整理资料并明确下一步工作",
            "owner": "演示负责人",
            "workspace": str(self.root / "workspace"),
            "stage": "资料准备",
            "currentWork": "准备接入资料",
            "nextStep": "负责人核对来源后登记项目",
            "sources": [{"path": str(self.product), "description": "项目说明来源"}],
            "milestones": [{"id": "demo-docs-prep", "title": "资料准备", "summary": "明确边界"}],
            "tasks": [{
                "taskId": "demo-docs-task-001",
                "title": "整理项目资料",
                "goal": "形成有来源的项目接入资料",
                "milestoneId": "demo-docs-prep",
                "category": "planned",
                "status": "planned",
                "member": "演示资料整理员",
                "verification": "合成样例，未执行任何真实任务",
                "boardSummary": dict(TASK_SUMMARY),
                "cardRefs": [],
                "documents": [{
                    "name": "来源报告", "kind": "report", "sourcePath": str(self.report),
                }],
                "history": [],
            }],
            "issues": [],
        }
        doc = {
            "schemaVersion": "project-intake.v1",
            "intakeId": "demo-intake-001",
            "intakeRevision": 1,
            "preparedAt": "2026-10-05T10:00:00+08:00",
            "preparedBy": dict(IDENTITY),
            "projects": [project],
        }
        doc.update(over)
        return doc

    def write_doc(self, obj, name="project-intake.json"):
        """返回绝对路径字符串（预检入口只接受 str，Path 一律按非法路径拒绝）。"""
        path = self.root / name
        path.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
        return str(path)

    def preflight(self, obj=None, **kwargs):
        path = self.write_doc(obj if obj is not None else self.intake_object())
        return preflight_document(path, allowed_roots=[str(self.root)], **kwargs)


class SourcePreflightTests(PreflightCase):
    def test_all_sources_read_and_applicable(self):
        result = self.preflight()
        self.assertTrue(result["structure"]["ok"])
        self.assertEqual(result["structure"]["errorCount"], 0)
        self.assertTrue(result["sourcePreflight"]["allReadable"])
        self.assertTrue(result["applicable"])
        self.assertEqual(result["notApplicableReasons"], [])
        self.assertEqual([item["status"] for item in result["sourcePreflight"]["items"]],
                         ["read", "read"])
        self.assertFalse(result["writePreview"]["wouldWrite"])
        self.assertFalse(result["writePreview"]["wouldDispatch"])
        self.assertFalse(self.db.exists())

    def test_missing_source_does_not_invalidate_structure(self):
        doc = self.intake_object()
        doc["projects"][0]["sources"][0]["path"] = str(self.sources / "absent.md")
        result = self.preflight(doc)
        self.assertTrue(result["structure"]["ok"])
        item = result["sourcePreflight"]["items"][0]
        self.assertEqual(item["status"], "unreadable")
        self.assertEqual(item["reason"], "document_not_file")
        self.assertFalse(result["applicable"])
        self.assertIn("sources_not_all_readable", result["notApplicableReasons"])

    def test_placeholders_are_pending_not_applicable(self):
        doc = self.intake_object()
        doc["projects"][0]["sources"][0]["path"] = "/REPLACE_WITH_APPROVED_ROOT/demo/product.md"
        result = self.preflight(doc)
        self.assertTrue(result["structure"]["ok"])
        item = result["sourcePreflight"]["items"][0]
        self.assertEqual(item["status"], "pending")
        self.assertEqual(item["reason"], "template_placeholder")
        self.assertFalse(result["applicable"])
        self.assertIn("template_placeholders_present", result["notApplicableReasons"])
        self.assertEqual(result["placeholders"][0]["path"], "projects[0].sources[0].path")

    def test_structure_error_and_source_result_are_separate(self):
        doc = self.intake_object()
        del doc["preparedAt"]
        result = self.preflight(doc)
        self.assertFalse(result["structure"]["ok"])
        self.assertEqual(result["structure"]["errors"]["intake"]["code"],
                         "intake_structure_invalid")
        self.assertEqual([item["status"] for item in result["sourcePreflight"]["items"]],
                         ["read", "read"])
        self.assertIn("structure_invalid", result["notApplicableReasons"])

    def test_source_outside_allowed_root_is_out_of_scope(self):
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / "product.md"
            external.write_text("不在允许根内", encoding="utf-8")
            doc = self.intake_object()
            doc["projects"][0]["sources"][0]["path"] = str(external)
            result = self.preflight(doc)
        item = result["sourcePreflight"]["items"][0]
        self.assertEqual(item["status"], "out_of_scope")
        self.assertEqual(item["reason"], "path_escape")

    def test_symlink_source_rejected(self):
        link = self.sources / "linked.md"
        link.symlink_to(self.product)
        doc = self.intake_object()
        doc["projects"][0]["sources"][0]["path"] = str(link)
        result = self.preflight(doc)
        item = result["sourcePreflight"]["items"][0]
        self.assertEqual(item["status"], "rejected")
        self.assertEqual(item["reason"], "path_symlink_rejected")

    def test_credential_source_rejected(self):
        secret_dir = self.root / ".ssh"
        secret_dir.mkdir()
        secret = secret_dir / "id_rsa"
        secret.write_text("合成占位，不是真实密钥", encoding="utf-8")
        doc = self.intake_object()
        doc["projects"][0]["sources"][0]["path"] = str(secret)
        result = self.preflight(doc)
        item = result["sourcePreflight"]["items"][0]
        self.assertEqual(item["status"], "rejected")
        self.assertEqual(item["reason"], "credential_or_private_source_denied")

    def test_directory_source_rejected(self):
        doc = self.intake_object()
        doc["projects"][0]["sources"][0]["path"] = str(self.sources)
        result = self.preflight(doc)
        item = result["sourcePreflight"]["items"][0]
        self.assertEqual(item["status"], "unreadable")
        self.assertEqual(item["reason"], "document_not_file")

    def test_oversized_material_rejected(self):
        big = self.sources / "big.md"
        big.write_text("x" * 600000, encoding="utf-8")
        doc = self.intake_object()
        doc["projects"][0]["sources"][0]["path"] = str(big)
        result = self.preflight(doc)
        item = result["sourcePreflight"]["items"][0]
        self.assertEqual(item["status"], "unreadable")
        self.assertEqual(item["reason"], "document_too_large")
        self.assertEqual(item["bytes"], 600000)

    def test_allowed_roots_required(self):
        path = self.write_doc(self.intake_object())
        with self.assertRaises(ProjectIntakeContractError) as ctx:
            preflight_document(path, allowed_roots=None)
        self.assertEqual(ctx.exception.code, "intake_not_configured")
        with self.assertRaises(ProjectIntakeContractError) as ctx:
            preflight_document(path, allowed_roots=["relative/root"])
        self.assertEqual(ctx.exception.code, "intake_not_configured")

    def test_input_file_itself_must_be_readable(self):
        with self.assertRaises(DocumentContractError) as ctx:
            preflight_document(str(self.root / "absent.json"), allowed_roots=[str(self.root)])
        self.assertEqual(ctx.exception.code, "document_not_file")

    def test_document_type_auto_detect_and_mismatch(self):
        result = self.preflight()
        self.assertEqual(result["documentType"], "intake")
        result = self.preflight(doc_type="intake")
        self.assertEqual(result["documentType"], "intake")
        with self.assertRaises(ProjectIntakeContractError) as ctx:
            self.preflight(doc_type="unknown")
        self.assertEqual(ctx.exception.code, "intake_structure_invalid")

    def test_unknown_schema_version_requires_explicit_kind(self):
        doc = self.intake_object(schemaVersion="project-intake.v2")
        path = self.write_doc(doc)
        with self.assertRaises(ProjectIntakeContractError) as ctx:
            preflight_document(path, allowed_roots=[str(self.root)])
        self.assertEqual(ctx.exception.code, "intake_structure_invalid")
        result = preflight_document(path, doc_type="intake", allowed_roots=[str(self.root)])
        self.assertEqual(result["structure"]["errors"]["schemaVersion"]["code"],
                         "schema_too_new")

    def test_source_files_and_database_unchanged(self):
        before = hashlib.sha256(self.product.read_bytes()).hexdigest()
        result = self.preflight()
        after = hashlib.sha256(self.product.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertFalse(self.db.exists())
        self.assertTrue(result["sourcePreflight"]["items"][0]["sha256"].startswith("sha256:"))
        self.assertFalse(any(result["sideEffects"].values()))

    def test_snapshot_is_stable_and_comparable(self):
        first = self.preflight()
        second = self.preflight()
        self.assertEqual(snapshot_materials(first["sourcePreflight"]["items"]),
                         snapshot_materials(second["sourcePreflight"]["items"]))
        self.assertEqual(material_digest(first["sourcePreflight"]["items"]),
                         material_digest(second["sourcePreflight"]["items"]))
        self.assertEqual(compare_material_snapshots(
            first["sourcePreflight"]["snapshot"], second["sourcePreflight"]["snapshot"]), [])
        # 来源变化后必须能被下一卡检出
        self.product.write_text("# 项目说明\n\n来源已更新。\n", encoding="utf-8")
        third = self.preflight()
        drift = compare_material_snapshots(
            first["sourcePreflight"]["snapshot"], third["sourcePreflight"]["snapshot"])
        self.assertEqual(len(drift), 1)
        self.assertEqual(drift[0]["change"], "changed")
        self.assertEqual(drift[0]["pointer"], "projects[0].sources[0]")


class UpdatePreflightTests(PreflightCase):
    def update_object(self, **over):
        doc = {
            "schemaVersion": "project-update.v1",
            "updateId": "demo-update-001",
            "updateRevision": 1,
            "projectId": "demo-docs",
            "taskId": "demo-docs-task-001",
            "expectedProjectRevision": 1,
            "kind": "progress",
            "occurredAt": "2026-10-05T11:00:00+08:00",
            "reportedBy": dict(IDENTITY),
            "sourceCard": {"cardId": "DEMO-DOCS-001", "cardRevision": 1,
                           "path": str(self.card)},
            "sourceReport": {"reportId": "demo-docs-report-001", "reportRevision": 1,
                             "path": str(self.report)},
            "boardSummary": dict(REPORT_SUMMARY),
            "businessSummary": {
                "summary": "合成进展声明，不作为真实完成证据",
                "owner": "演示资料整理员",
                "unfinished": ["替换合成来源"],
                "impact": ["仅展示普通进展"],
                "nextStep": "换成真实报告后走受控入口",
            },
            "evidence": [{"kind": "file", "detail": "合成占位资料", "level": "unverified"}],
        }
        doc.update(over)
        return doc

    def test_update_preflight_reads_declared_sources(self):
        path = self.write_doc(self.update_object(), name="project-update.json")
        result = preflight_document(path, allowed_roots=[str(self.root)])
        self.assertEqual(result["documentType"], "update")
        self.assertTrue(result["structure"]["ok"])
        self.assertEqual([item["role"] for item in result["sourcePreflight"]["items"]],
                         ["card", "report"])
        self.assertTrue(result["applicable"])
        self.assertEqual(result["summary"]["updateId"], "demo-update-001")

    def test_update_rejects_run_fields_before_preflight(self):
        path = self.write_doc(self.update_object(attemptId="att-1"), name="bad-update.json")
        result = preflight_document(path, allowed_roots=[str(self.root)])
        errors = result["structure"]["errors"]
        self.assertEqual(errors["update.forbiddenFields"]["code"],
                         "update_run_field_forbidden")
        self.assertFalse(result["applicable"])


class CliInspectTests(PreflightCase):
    def cli(self, *extra):
        return d4ctl.run([
            "--db", str(self.db),
            "--allowed-root", str(self.root),
            "project", "inspect", *extra,
        ])

    def test_cli_inspect_ok_without_database(self):
        path = self.write_doc(self.intake_object())
        code, result = self.cli("--file", str(path))
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        self.assertEqual(result["cmd"], "project inspect")
        data = result["data"]
        self.assertTrue(data["structure"]["ok"])
        self.assertFalse(data["writePreview"]["wouldWrite"])
        self.assertFalse(data["writePreview"]["wouldDispatch"])
        self.assertFalse(data["sideEffects"]["databaseOpened"])
        self.assertFalse(self.db.exists())

    def test_cli_rejects_relative_file_path(self):
        code, result = self.cli("--file", "project-intake.json")
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "usage_error")

    def test_cli_reports_structure_error_with_code(self):
        path = self.write_doc(self.intake_object(schemaVersion="unknown.v9"))
        code, result = self.cli("--file", str(path), "--kind", "intake")
        self.assertEqual(code, 0)
        self.assertEqual(result["data"]["structure"]["errors"]["schemaVersion"]["code"],
                         "schema_too_new")

    def test_cli_bad_json_is_fail_closed(self):
        path = self.root / "broken.json"
        path.write_text("{不是 JSON", encoding="utf-8")
        code, result = self.cli("--file", str(path))
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "intake_structure_invalid")
        self.assertFalse(self.db.exists())

    def test_existing_query_behaviour_unchanged(self):
        # 既有只读子命令仍要求数据库存在，且不会被 project inspect 改动影响
        code, result = d4ctl.run([
            "--db", str(self.db),
            "--allowed-root", str(self.root),
            "query", "--project", "demo-docs",
        ])
        self.assertEqual(code, 1)
        self.assertEqual(result["error"]["code"], "usage_error")
        self.assertFalse(self.db.exists())

    def test_existing_document_inspect_unchanged(self):
        card = self.root / "card.md"
        card.write_text("---\nschemaVersion: task-card-board.v1\n---\n正文\n", encoding="utf-8")
        code, result = d4ctl.run([
            "--db", str(self.db),
            "--allowed-root", str(self.root),
            "document", "inspect", "--file", str(card), "--kind", "task-card",
        ])
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "document_invalid")
        self.assertIn("errors", result["error"]["details"])


if __name__ == "__main__":
    unittest.main()
