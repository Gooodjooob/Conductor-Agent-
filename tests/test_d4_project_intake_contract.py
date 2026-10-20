"""D4-PROJECT-01 定向测试：project-intake.v1 / project-update.v1 结构合同。

覆盖任务卡验收的结构面：
- 合法结构可检查；缺字段、重复键、未知版本、超限、错误类型、越界、
  重复项目/任务/卡关联、材料索引错误均有确定结果；
- 摘要复用既有看板校验（长度、未知字段、技术词泄漏）；
- 更新格式不接受运行 / 授权字段；证据分级走既有词表。

本文件只测纯校验，不读文件、不落盘。
"""
import json
import unittest

from contracts.project_intake import (
    MATERIAL_MAX_BYTES,
    ProjectIntakeContractError,
    collect_material_paths,
    detect_schema_version,
    find_placeholders,
    parse_intake_json,
    summarize_intake,
    validate_intake_document,
    validate_update_document,
)

TASK_SUMMARY = {
    "title": "整理项目资料",
    "stage": "计划准备接入资料",
    "problem": "实际来源尚待填写",
    "nextStep": "整理员补齐资料后交负责人检查",
}
REPORT_SUMMARY = {
    "stage": "资料整理中",
    "result": "成员报告已交回，待检查",
    "problem": "实际来源仍待核对",
    "nextStep": "负责人核对资料后决定下一步",
}
IDENTITY = {
    "alias": "演示资料整理员",
    "role": "docs-worker",
    "host": "demo-host",
    "agentId": "demo-docs-worker",
}


def make_issue(**over):
    issue = {
        "issueId": "demo-docs-issue-001",
        "taskId": "demo-docs-task-001",
        "title": "接入来源尚未准备",
        "impact": "资料不能正式登记",
        "owner": "演示资料整理员",
        "status": "open",
        "nextStep": "补齐真实来源并由负责人核对后恢复登记",
    }
    issue.update(over)
    return issue


def make_task(**over):
    task = {
        "taskId": "demo-docs-task-001",
        "title": "整理项目资料",
        "goal": "形成有来源的项目接入资料",
        "milestoneId": "demo-docs-prep",
        "category": "planned",
        "status": "planned",
        "member": "演示资料整理员",
        "verification": "虚构格式样例，未执行任何真实任务",
        "boardSummary": dict(TASK_SUMMARY),
        "cardRefs": [],
        "documents": [{
            "name": "来源任务卡",
            "kind": "task-card",
            "sourcePath": "/tmp/approved/demo-docs/task-card.md",
        }],
        "history": [],
    }
    task.update(over)
    return task


def make_project(**over):
    project = {
        "projectId": "demo-docs",
        "name": "演示资料整理项目",
        "goal": "整理资料并明确下一步工作",
        "owner": "演示负责人",
        "workspace": "/tmp/approved/demo-docs",
        "stage": "资料准备",
        "currentWork": "准备接入资料",
        "nextStep": "负责人核对来源后登记项目",
        "sources": [{
            "path": "/tmp/approved/demo-docs/product.md",
            "description": "项目说明来源",
        }],
        "milestones": [{
            "id": "demo-docs-prep",
            "title": "资料准备",
            "summary": "明确项目和任务边界",
        }],
        "tasks": [make_task()],
        "issues": [make_issue()],
    }
    project.update(over)
    return project


def make_intake(**over):
    doc = {
        "schemaVersion": "project-intake.v1",
        "intakeId": "demo-intake-001",
        "intakeRevision": 1,
        "preparedAt": "2026-10-03T09:00:00+08:00",
        "preparedBy": dict(IDENTITY),
        "projects": [make_project()],
    }
    doc.update(over)
    return doc


def make_update(**over):
    doc = {
        "schemaVersion": "project-update.v1",
        "updateId": "demo-update-001",
        "updateRevision": 1,
        "projectId": "demo-docs",
        "taskId": "demo-docs-task-001",
        "expectedProjectRevision": 1,
        "kind": "progress",
        "occurredAt": "2026-10-03T10:00:00+08:00",
        "reportedBy": dict(IDENTITY),
        "sourceCard": {
            "cardId": "DEMO-DOCS-001",
            "cardRevision": 1,
            "path": "/tmp/approved/demo-docs/task-card.md",
        },
        "sourceReport": {
            "reportId": "demo-docs-report-001",
            "reportRevision": 1,
            "path": "/tmp/approved/demo-docs/progress-report.md",
        },
        "boardSummary": dict(REPORT_SUMMARY),
        "businessSummary": {
            "summary": "仅示意进展声明，不能作为真实完成证据",
            "owner": "演示资料整理员",
            "unfinished": ["替换实际来源"],
            "impact": ["展示普通进展，不修改正式运行状态"],
            "nextStep": "换成真实报告后提交受控更新入口",
        },
        "evidence": [
            {"kind": "file", "detail": "演示占位资料，未读取实际来源", "level": "unverified"}
        ],
    }
    doc.update(over)
    return doc


class IntakeStructureTests(unittest.TestCase):
    def test_valid_intake_passes(self):
        self.assertEqual(validate_intake_document(make_intake()), {})

    def test_missing_required_field(self):
        doc = make_intake()
        del doc["preparedAt"]
        errors = validate_intake_document(doc)
        self.assertEqual(errors["intake"]["code"], "intake_structure_invalid")
        self.assertIn("preparedAt", errors["intake"]["missing"])

    def test_missing_project_required_field(self):
        doc = make_intake()
        del doc["projects"][0]["nextStep"]
        errors = validate_intake_document(doc)
        self.assertEqual(errors["projects[0]"]["code"], "intake_structure_invalid")
        self.assertIn("nextStep", errors["projects[0]"]["missing"])

    def test_empty_projects_and_sources_rejected(self):
        doc = make_intake(projects=[])
        self.assertEqual(validate_intake_document(doc)["projects"]["code"],
                         "intake_structure_invalid")
        doc = make_intake()
        doc["projects"][0]["sources"] = []
        self.assertEqual(validate_intake_document(doc)["projects[0].sources"]["code"],
                         "intake_structure_invalid")

    def test_unknown_version_and_cross_version(self):
        doc = make_intake(schemaVersion="project-intake.v2")
        self.assertEqual(validate_intake_document(doc)["schemaVersion"]["code"],
                         "schema_too_new")
        doc = make_intake(schemaVersion="project-update.v1")
        self.assertEqual(validate_intake_document(doc)["schemaVersion"]["code"],
                         "intake_schema_mismatch")
        self.assertEqual(validate_update_document(make_update(schemaVersion="project-intake.v1"))
                         ["schemaVersion"]["code"], "intake_schema_mismatch")

    def test_unknown_field_rejected(self):
        doc = make_intake()
        doc["projects"][0]["extraNote"] = "未冻结字段"
        errors = validate_intake_document(doc)
        self.assertEqual(errors["projects[0]"]["code"], "intake_unknown_field")
        self.assertIn("extraNote", errors["projects[0]"]["fields"])

    def test_bool_as_integer_rejected(self):
        doc = make_intake(intakeRevision=True)
        self.assertEqual(validate_intake_document(doc)["intakeRevision"]["code"],
                         "intake_type_invalid")
        doc = make_update(expectedProjectRevision=True)
        self.assertEqual(validate_update_document(doc)["expectedProjectRevision"]["code"],
                         "intake_type_invalid")

    def test_invalid_identifiers(self):
        doc = make_intake()
        doc["projects"][0]["projectId"] = "-demo"
        self.assertEqual(validate_intake_document(doc)["projects[0].projectId"]["code"],
                         "intake_identifier_invalid")
        doc = make_intake()
        doc["projects"][0]["tasks"][0]["taskId"] = "带空格 的编号"
        self.assertEqual(validate_intake_document(doc)["projects[0].tasks[0].taskId"]["code"],
                         "intake_identifier_invalid")

    def test_relative_and_escaping_paths_rejected(self):
        doc = make_intake()
        doc["projects"][0]["sources"][0]["path"] = "product.md"
        self.assertEqual(validate_intake_document(doc)["projects[0].sources[0].path"]["code"],
                         "path_not_absolute")
        doc = make_intake()
        doc["projects"][0]["workspace"] = "/tmp/approved/../etc"
        self.assertEqual(validate_intake_document(doc)["projects[0].workspace"]["code"],
                         "path_escape")

    def test_enum_and_milestone_binding(self):
        doc = make_intake()
        doc["projects"][0]["tasks"][0]["category"] = "bugfix"
        self.assertEqual(validate_intake_document(doc)["projects[0].tasks[0].category"]["code"],
                         "intake_enum_invalid")
        doc = make_intake()
        doc["projects"][0]["tasks"][0]["milestoneId"] = "unknown-stage"
        self.assertEqual(validate_intake_document(doc)["projects[0].tasks[0].milestoneId"]["code"],
                         "intake_structure_invalid")
        doc = make_intake()
        doc["projects"][0]["issues"][0]["status"] = "pending"
        self.assertEqual(validate_intake_document(doc)["projects[0].issues[0].status"]["code"],
                         "intake_enum_invalid")

    def test_duplicate_relations_rejected(self):
        doc = make_intake()
        doc["projects"].append(make_project())
        self.assertEqual(validate_intake_document(doc)["projects"]["code"],
                         "intake_relation_duplicate")
        doc = make_intake()
        second = make_project(projectId="other-project")
        doc["projects"].append(second)
        self.assertEqual(validate_intake_document(doc)["projects.tasks"]["code"],
                         "intake_relation_duplicate")
        doc = make_intake()
        doc["projects"][0]["issues"].append(make_issue())
        self.assertEqual(validate_intake_document(doc)["projects[0].issues"]["code"],
                         "intake_relation_duplicate")

    def test_card_revision_rules(self):
        doc = make_intake()
        task = doc["projects"][0]["tasks"][0]
        task["category"] = "implementation"
        task["cardRefs"] = [{"cardId": "DEMO-DOCS-001", "cardRevision": 1,
                             "path": "/tmp/approved/demo-docs/task-card.md"}]
        self.assertEqual(validate_intake_document(doc), {})
        doc["projects"][0]["tasks"][0]["cardRefs"][0]["cardRevision"] = "unverified"
        self.assertEqual(validate_intake_document(doc), {})
        doc["projects"][0]["tasks"][0]["cardRefs"][0]["cardRevision"] = 0
        self.assertEqual(
            validate_intake_document(doc)["projects[0].tasks[0].cardRefs[0].cardRevision"]["code"],
            "intake_card_revision_invalid")
        doc["projects"][0]["tasks"][0]["cardRefs"][0]["cardRevision"] = True
        self.assertEqual(
            validate_intake_document(doc)["projects[0].tasks[0].cardRefs[0].cardRevision"]["code"],
            "intake_card_revision_invalid")

    def test_executed_task_needs_card_ref(self):
        doc = make_intake()
        task = doc["projects"][0]["tasks"][0]
        task["category"] = "implementation"
        task["status"] = "verified"
        errors = validate_intake_document(doc)
        self.assertEqual(errors["projects[0].tasks[0].cardRefs"]["code"],
                         "intake_structure_invalid")

    def test_material_index_bounds(self):
        doc = make_intake()
        doc["projects"][0]["tasks"][0]["history"] = [{
            "title": "第一次返工",
            "note": "按复核意见修正范围",
            "documentIndexes": [1],
        }]
        errors = validate_intake_document(doc)
        self.assertEqual(
            errors["projects[0].tasks[0].history[0].documentIndexes[0]"]["code"],
            "intake_material_index_invalid")
        doc = make_intake()
        doc["projects"][0]["tasks"][0]["history"] = [{
            "title": "第一次返工",
            "note": "按复核意见修正范围",
            "documentIndexes": [True],
        }]
        errors = validate_intake_document(doc)
        self.assertEqual(
            errors["projects[0].tasks[0].history[0].documentIndexes[0]"]["code"],
            "intake_material_index_invalid")

    def test_issue_resolution_evidence(self):
        doc = make_intake()
        doc["projects"][0]["issues"][0]["status"] = "closed"
        errors = validate_intake_document(doc)
        self.assertEqual(errors["projects[0].issues[0].resolutionEvidence"]["code"],
                         "intake_issue_resolution_missing")
        doc["projects"][0]["issues"][0]["resolutionEvidence"] = [0]
        self.assertEqual(validate_intake_document(doc), {})
        doc["projects"][0]["issues"][0]["resolutionEvidence"] = [7]
        errors = validate_intake_document(doc)
        self.assertEqual(errors["projects[0].issues[0].resolutionEvidence[0]"]["code"],
                         "intake_material_index_invalid")

    def test_collection_limits(self):
        doc = make_intake()
        doc["projects"][0]["sources"] = [
            {"path": "/tmp/approved/demo-docs/s%d.md" % index, "description": "来源"}
            for index in range(51)
        ]
        errors = validate_intake_document(doc)
        self.assertEqual(errors["projects[0].sources"]["code"], "intake_limit_exceeded")
        self.assertEqual(errors["projects[0].sources"]["limit"], 50)

    def test_board_summary_reuses_existing_rules(self):
        doc = make_intake()
        doc["projects"][0]["tasks"][0]["boardSummary"] = dict(TASK_SUMMARY, extra="多余字段")
        self.assertEqual(validate_intake_document(doc)["boardSummary.unknown"]["code"],
                         "board_summary_unknown_field")
        doc = make_intake()
        doc["projects"][0]["tasks"][0]["boardSummary"] = dict(
            TASK_SUMMARY, title="命中 invalid_task 的错误码写法")
        self.assertEqual(validate_intake_document(doc)["boardSummary.title"]["code"],
                         "board_summary_tech_leak")
        doc = make_intake()
        doc["projects"][0]["tasks"][0]["boardSummary"] = dict(TASK_SUMMARY, title="字" * 121)
        self.assertEqual(validate_intake_document(doc)["boardSummary.title"]["code"],
                         "board_summary_too_long")

    def test_parse_rejects_non_json_and_duplicate_keys(self):
        with self.assertRaises(ProjectIntakeContractError) as ctx:
            parse_intake_json("{不是 JSON")
        self.assertEqual(ctx.exception.code, "intake_structure_invalid")
        with self.assertRaises(ProjectIntakeContractError) as ctx:
            parse_intake_json('{"intakeId": "a", "intakeId": "b"}')
        self.assertEqual(ctx.exception.code, "duplicate_key")

    def test_parse_rejects_oversized_text(self):
        with self.assertRaises(ProjectIntakeContractError) as ctx:
            parse_intake_json(json.dumps(make_intake()) + " " * (2 * 1024 * 1024))
        self.assertEqual(ctx.exception.code, "intake_too_large")

    def test_helpers(self):
        doc = make_intake()
        self.assertEqual(detect_schema_version(doc), "intake")
        self.assertEqual(detect_schema_version(make_update()), "update")
        self.assertIsNone(detect_schema_version({"schemaVersion": "other.v9"}))
        summary = summarize_intake(doc)
        self.assertEqual((summary["projects"], summary["tasks"], summary["issues"]),
                         (1, 1, 1))
        paths = collect_material_paths(doc)
        # 计划任务 cardRefs 为空，因此只有 sources + documents 两类来源
        self.assertEqual([item["role"] for item in paths], ["source", "material"])
        self.assertEqual(find_placeholders(doc), [])


class UpdateStructureTests(unittest.TestCase):
    def test_valid_update_passes(self):
        self.assertEqual(validate_update_document(make_update()), {})

    def test_run_and_authorization_fields_forbidden(self):
        for field, value in (("attemptId", "att-1"), ("hostRunId", "run-1"),
                             ("approved", True), ("accepted", True),
                             ("permissions", {"writeScope": ["receipts/"]})):
            with self.subTest(field=field):
                doc = make_update()
                doc[field] = value
                errors = validate_update_document(doc)
                self.assertEqual(errors["update.forbiddenFields"]["code"],
                                 "update_run_field_forbidden")
                self.assertIn(field, errors["update.forbiddenFields"]["fields"])

    def test_forbidden_alias_fields_also_rejected(self):
        doc = make_update(sessionId="qclaw-session-1")
        errors = validate_update_document(doc)
        self.assertEqual(errors["update.forbiddenFields"]["code"],
                         "update_run_field_forbidden")

    def test_kind_enum_and_missing_evidence(self):
        doc = make_update(kind="approved")
        self.assertEqual(validate_update_document(doc)["kind"]["code"], "intake_enum_invalid")
        doc = make_update()
        del doc["evidence"]
        self.assertEqual(validate_update_document(doc)["evidence"]["code"],
                         "evidence_invalid")
        doc = make_update(evidence=[{"kind": "file", "detail": "本地观测", "level": "LOCAL"}])
        self.assertEqual(validate_update_document(doc)["evidence[0].level"]["code"],
                         "evidence_invalid")

    def test_update_board_summary_uses_report_field_set(self):
        doc = make_update(boardSummary=dict(TASK_SUMMARY))
        errors = validate_update_document(doc)
        self.assertEqual(errors["boardSummary.unknown"]["code"],
                         "board_summary_unknown_field")

    def test_supersedes_update_id_type(self):
        doc = make_update(supersedesUpdateId=7)
        self.assertEqual(validate_update_document(doc)["supersedesUpdateId"]["code"],
                         "intake_type_invalid")
        doc = make_update(supersedesUpdateId="demo-update-000")
        self.assertEqual(validate_update_document(doc), {})

    def test_source_card_and_report_required(self):
        doc = make_update()
        del doc["sourceCard"]["cardRevision"]
        self.assertEqual(validate_update_document(doc)["sourceCard"]["code"],
                         "intake_structure_invalid")
        doc = make_update()
        doc["sourceReport"]["reportRevision"] = "1"
        self.assertEqual(validate_update_document(doc)["sourceReport.reportRevision"]["code"],
                         "intake_type_invalid")

    def test_material_max_bytes_is_service_constant(self):
        # 材料上限由服务侧常量承载，不从 payload 取；此处只锁定契约常量存在且为 512 KiB
        self.assertEqual(MATERIAL_MAX_BYTES, 512 * 1024)


if __name__ == "__main__":
    unittest.main()
