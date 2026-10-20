"""D4-NEXT-01A 定向测试：任务卡与回执的双层字段合同（P2A-1）。

覆盖：
- 任务卡/回执正例（字段冻结集）
- 负例：缺字段、类型错误、重复键、身份冲突、来源卡不匹配、
  版本过新、resume 未支持、摘要技术词泄漏、报告缺实际执行者
- 三层职责（boardSummary / businessSummary / evidence）互不替代
- 旧版 JSON 兼容：缺摘要只标 pending，不伪造
- 纯合同测试：不调用宿主、不写正式数据库、不依赖网络。
"""
import unittest

from contracts.board_summary import (
    TASK_CARD_BOARD_FIELDS,
    REPORT_BOARD_FIELDS,
    BOARD_ERROR_CODES,
    BoardContractError,
    detect_tech_leak,
    validate_task_card_board_summary,
    validate_report_board_summary,
    validate_business_summary,
    validate_evidence,
    validate_identity_block,
    validate_report_header,
    validate_session_mode,
    validate_schema_version,
    detect_duplicate_keys,
    normalize_legacy_object,
    # D4-NEXT-01A-R1：整体校验与版本-文档类型绑定
    validate_schema_version_for_doc,
    validate_task_card_document,
    validate_report_document,
)


def task_card_board(**over):
    base = {
        "title": "统一任务卡和回执的展示规则",
        "stage": "准备确定展示和技术记录的分工",
        "problem": "暂无",
        "nextStep": "候选成员整理规则后交给负责人复核",
    }
    base.update(over)
    return base


def report_board(**over):
    base = {
        "stage": "字段规则已经整理完成",
        "result": "确定了任务卡和报告各自要写的字段",
        "problem": "暂无",
        "nextStep": "等负责人检查后决定下一步",
    }
    base.update(over)
    return base


def report_header(**over):
    base = {
        "reportId": "rpt-1",
        "reportRevision": 1,
        "reportType": "qa",
        "projectId": "conductor-d4",
        "taskId": "t-1",
        "attemptId": "att-1",
        "sourceCard": {
            "path": "/abs/path/card.md",
            "cardId": "D4-NEXT-01A",
            "cardRevision": 1,
        },
        "requestedBy": {"alias": "cox", "role": "sales-architect", "host": "Codex", "agentId": "cox"},
        "executedBy": {
            "alias": "qa-qclaw-1",
            "role": "qa-reporter",
            "host": "QClaw",
            "agentId": "qa-1",
            "environmentId": "qclaw-local",
        },
    }
    base.update(over)
    return base


class TaskCardBoardSummaryTests(unittest.TestCase):
    def test_positive_example_passes(self):
        self.assertEqual(validate_task_card_board_summary(task_card_board()), {})

    def test_field_set_frozen(self):
        self.assertEqual(TASK_CARD_BOARD_FIELDS, ("title", "stage", "problem", "nextStep"))

    def test_missing_field_reports_code(self):
        errors = validate_task_card_board_summary(
            task_card_board(**{"stage": None})
        )
        self.assertIn("boardSummary.stage", errors)
        self.assertEqual(errors["boardSummary.stage"]["code"], "board_summary_type")

    def test_missing_entire_summary(self):
        errors = validate_task_card_board_summary(None)
        self.assertIn("boardSummary", errors)

    def test_empty_value_rejected(self):
        errors = validate_task_card_board_summary(task_card_board(problem=" "))
        self.assertEqual(errors["boardSummary.problem"]["code"], "board_summary_empty")

    def test_too_long_rejected(self):
        errors = validate_task_card_board_summary(task_card_board(title="长" * 200))
        self.assertEqual(errors["boardSummary.title"]["code"], "board_summary_too_long")

    def test_unknown_field_rejected(self):
        errors = validate_task_card_board_summary(task_card_board(extra="x"))
        self.assertEqual(errors["boardSummary.unknown"]["code"], "board_summary_unknown_field")

    def test_tech_leak_error_code_path(self):
        errors = validate_task_card_board_summary(
            task_card_board(nextStep="先跑 d4ctl 再说")
        )
        self.assertEqual(errors["boardSummary.nextStep"]["code"], "board_summary_tech_leak")
        self.assertEqual(errors["boardSummary.nextStep"]["leak_kind"], "command_line")


class ReportBoardSummaryTests(unittest.TestCase):
    def test_positive_example_passes(self):
        self.assertEqual(validate_report_board_summary(report_board()), {})

    def test_field_set_frozen(self):
        self.assertEqual(REPORT_BOARD_FIELDS, ("stage", "result", "problem", "nextStep"))

    def test_result_field_missing_rejected(self):
        errors = validate_report_board_summary({"stage": "已完成", "problem": "暂无", "nextStep": "等待检查"})
        self.assertEqual(errors["boardSummary.result"]["code"], "board_summary_missing")


class TechLeakDetectionTests(unittest.TestCase):
    def test_detects_error_code(self):
        self.assertEqual(detect_tech_leak("出现了 invalid_event"), ("error_code", "invalid_event"))

    def test_detects_api_path(self):
        self.assertIsNotNone(detect_tech_leak("调了 /v1/tasks 接口"))
        self.assertIsNotNone(detect_tech_leak("文件在 /Users/demo/x.py"))

    def test_detects_internal_id(self):
        self.assertIsNotNone(detect_tech_leak("事件 evt-204-0003 重复"))

    def test_plain_chinese_passes(self):
        self.assertIsNone(detect_tech_leak("任务卡展示规则已经整理好，等负责人复核"))


class BusinessSummaryTests(unittest.TestCase):
    def test_valid(self):
        summary = {
            "summary": "冻结双层字段合同，含正负例与校验规则",
            "owner": "dev-qclaw-1",
            "unfinished": [],
            "impact": ["后续解析器与回执导入引用本合同"],
            "nextStep": "交 cox review",
        }
        self.assertEqual(validate_business_summary(summary), {})

    def test_missing_required_key(self):
        errors = validate_business_summary({"summary": "x", "owner": "y"})
        self.assertEqual(errors["businessSummary.nextStep"]["code"], "business_summary_invalid")

    def test_unknown_key_rejected(self):
        errors = validate_business_summary({
            "summary": "x", "owner": "y", "unfinished": [], "impact": [],
            "nextStep": "z", "sneaky": True,
        })
        self.assertEqual(errors["businessSummary.sneaky"]["code"], "business_summary_invalid")

    def test_ext_namespace_allowed(self):
        summary = {
            "summary": "x", "owner": "y", "unfinished": [], "impact": [],
            "nextStep": "z", "ext.reviewRound": 2,
        }
        self.assertEqual(validate_business_summary(summary), {})


class EvidenceTests(unittest.TestCase):
    def test_valid_evidence(self):
        evidence = [
            {"kind": "command", "detail": "隔离目录运行合同测试，全部通过", "level": "verified"},
            {"kind": "file", "detail": "新增合同校验模块", "level": "verified"},
        ]
        self.assertEqual(validate_evidence(evidence), {})

    def test_missing_level_rejected(self):
        errors = validate_evidence([{"kind": "command", "detail": "x"}])
        self.assertEqual(errors["evidence[0]"]["code"], "evidence_invalid")
        self.assertIn("level", errors["evidence[0]"]["missing"])

    def test_unknown_kind_rejected(self):
        errors = validate_evidence([{"kind": "vibes", "detail": "x", "level": "verified"}])
        self.assertEqual(errors["evidence[0].kind"]["code"], "evidence_invalid")

    def test_not_a_list_rejected(self):
        self.assertIn("evidence", validate_evidence("命令输出"))

    def test_business_summary_cannot_replace_evidence(self):
        # 三层职责：businessSummary 不能充当 evidence
        summary = {"summary": "已完成", "owner": "x", "unfinished": [], "impact": [], "nextStep": "y"}
        self.assertEqual(validate_business_summary(summary), {})
        self.assertIn("evidence", validate_evidence(summary))


class IdentityAndSourceTests(unittest.TestCase):
    def test_valid_identity_block(self):
        block = {"alias": "dev-qclaw-1", "role": "skill-worker", "host": "QClaw", "agentId": "dev-1"}
        self.assertEqual(validate_identity_block(block, "executor"), {})

    def test_alias_cannot_replace_agent_id(self):
        errors = validate_identity_block({"alias": "dev-qclaw-1", "host": "QClaw"}, "executor")
        self.assertEqual(errors["executor.agentId"]["code"], "identity_missing")

    def test_report_header_positive(self):
        self.assertEqual(validate_report_header(report_header()), {})

    def test_report_missing_executor_rejected(self):
        header = report_header(executedBy={"alias": "cox", "role": "sales-architect", "host": "Codex", "agentId": "cox"})
        errors = validate_report_header(header)
        self.assertEqual(errors["executedBy.environmentId"]["code"], "report_missing_executor")

    def test_source_card_mismatch(self):
        header = report_header(sourceCard={"path": "", "cardId": "", "cardRevision": None})
        errors = validate_report_header(header)
        self.assertEqual(errors["reportHeader.sourceCard.path"]["code"], "source_card_mismatch")

    def test_report_type_vocabulary(self):
        errors = validate_report_header(report_header(reportType="ad-hoc"))
        self.assertEqual(errors["reportHeader.reportType"]["code"], "identity_conflict")

    def test_missing_header_fields(self):
        errors = validate_report_header({"reportId": "rpt-1"})
        self.assertEqual(errors["reportHeader.missing"]["code"], "identity_missing")
        self.assertIn("sourceCard", errors["reportHeader.missing"]["fields"])

    def test_role_conflict_same_agent(self):
        header = report_header(
            requestedBy={"alias": "cox", "role": "sales-architect", "host": "Codex", "agentId": "shared-1"},
            executedBy={
                "alias": "qa-qclaw-1", "role": "qa-reporter", "host": "QClaw",
                "agentId": "shared-1", "environmentId": "qclaw-local",
            },
        )
        errors = validate_report_header(header)
        self.assertEqual(errors["requestedBy.agentId"]["code"], "identity_conflict")


class SessionModeTests(unittest.TestCase):
    def test_new_supported(self):
        self.assertEqual(validate_session_mode("new"), {})

    def test_resume_rejected_with_reason(self):
        errors = validate_session_mode("resume")
        self.assertEqual(errors["sessionMode"]["code"], "session_mode_unsupported")
        self.assertIn("reason", errors["sessionMode"])

    def test_unknown_mode_rejected(self):
        self.assertEqual(validate_session_mode("whatever")["sessionMode"]["code"], "identity_conflict")

    def test_missing_mode_rejected(self):
        self.assertEqual(validate_session_mode("")["sessionMode"]["code"], "identity_missing")


class SchemaVersionTests(unittest.TestCase):
    def test_supported_versions_pass(self):
        self.assertEqual(validate_schema_version("task-card-board.v1", ("task-card-board.v1",)), {})
        self.assertEqual(validate_schema_version("report-board.v1", ("task-card-board.v1", "report-board.v1")), {})

    def test_too_new_rejected_for_negotiation(self):
        errors = validate_schema_version("task-card-board.v2", ("task-card-board.v1",))
        self.assertEqual(errors["schemaVersion"]["code"], "schema_too_new")


class DuplicateKeyTests(unittest.TestCase):
    def test_detects_duplicate(self):
        pairs = [("cardId", "a"), ("cardId", "b"), ("title", "t")]
        self.assertEqual(detect_duplicate_keys(pairs), ["cardId"])

    def test_no_duplicates(self):
        self.assertEqual(detect_duplicate_keys([("a", 1), ("b", 2)], ), [])


class LegacyCompatTests(unittest.TestCase):
    def test_legacy_without_summary_marks_pending_not_forged(self):
        legacy = {"projectId": "conductor", "taskId": "t-1", "summary": "旧格式交付"}
        normalized, pending = normalize_legacy_object(legacy)
        self.assertEqual(pending, ["boardSummary", "businessSummary"])
        # 旧字段语义不变
        self.assertEqual(normalized["taskId"], "t-1")
        # 不伪造摘要：normalized 中没有凭空生成的 boardSummary
        self.assertNotIn("boardSummary", normalized)

    def test_new_object_no_pending(self):
        obj = {"boardSummary": {"title": "x"}, "businessSummary": {"summary": "y"}}
        _normalized, pending = normalize_legacy_object(obj)
        self.assertEqual(pending, [])


# ---------------------------------------------------------------------------
# D4-NEXT-01A-R1：整体校验与版本-文档类型绑定
# ---------------------------------------------------------------------------

def task_card_doc(**over):
    """任务卡整体正例（新版 schema，来源头完整；R2 起含顶层 title）。"""
    base = {
        "schemaVersion": "task-card-board.v1",
        "cardId": "D4-NEXT-01A-R1",
        "cardRevision": 1,
        "projectId": "conductor-d4",
        # D4-NEXT-01A-R2：顶层 title 成为必填，合法卡夹具同步补齐
        "title": "统一任务卡和回执的展示规则",
        "issuer": {"alias": "cox", "role": "sales-architect", "host": "Codex", "agentId": "cox"},
        "executor": {
            "alias": "dev-wkb1", "role": "skill-worker", "host": "WorkBuddy",
            "agentId": "dev-wkb1", "environmentId": "workbuddy-local",
        },
        "reviewer": {"alias": "cox", "role": "sales-architect"},
        "sessionMode": "new",
        "boardSummary": task_card_board(),
    }
    base.update(over)
    return base


def full_report_doc(**over):
    """报告整体正例（新版 schema，运行事实 + 三层内容齐全）。"""
    base = {
        "schemaVersion": "report-board.v1",
        "reportId": "rpt-r1-1",
        "reportRevision": 1,
        "reportType": "qa",
        "projectId": "conductor-d4",
        "taskId": "t-1",
        "attemptId": "att-1",
        "adapter": "codex-adapter",
        "sessionId": "unverified",
        "hostRunId": "unverified",
        "sourceCard": {"path": "/abs/path/card.md", "cardId": "D4-NEXT-01A-R1", "cardRevision": 1},
        "requestedBy": {"alias": "cox", "role": "sales-architect", "host": "Codex", "agentId": "cox"},
        "executedBy": {
            "alias": "dev-wkb1", "role": "skill-worker", "host": "WorkBuddy",
            "agentId": "dev-wkb1", "environmentId": "workbuddy-local",
        },
        "reviewedBy": {"alias": "cox", "role": "sales-architect"},
        "businessSummary": {
            "summary": "补齐整体校验与负例测试",
            "owner": "dev-wkb1",
            "unfinished": [],
            "impact": ["缺少关键字段的报告不再放行"],
            "nextStep": "交 cox review",
        },
        "evidence": [
            {"kind": "test", "detail": "隔离目录运行合同测试，全部通过", "level": "verified"},
        ],
        "boardSummary": report_board(),
    }
    base.update(over)
    return base


class SchemaVersionDocBindingTests(unittest.TestCase):
    """R1：schema 版本按文档类型绑定，交叉使用明确拒绝。"""

    def test_task_card_accepts_only_task_card_version(self):
        self.assertEqual(validate_schema_version_for_doc("task-card-board.v1", "task-card"), {})
        self.assertNotEqual(validate_schema_version_for_doc("report-board.v1", "task-card"), {})

    def test_report_accepts_only_report_version(self):
        self.assertEqual(validate_schema_version_for_doc("report-board.v1", "report"), {})
        self.assertNotEqual(validate_schema_version_for_doc("task-card-board.v1", "report"), {})

    def test_cross_use_rejected_with_doc_mismatch(self):
        errors = validate_schema_version_for_doc("report-board.v1", "task-card")
        self.assertEqual(errors["schemaVersion"]["code"], "schema_doc_mismatch")
        errors = validate_schema_version_for_doc("task-card-board.v1", "report")
        self.assertEqual(errors["schemaVersion"]["code"], "schema_doc_mismatch")

    def test_too_new_returns_schema_too_new(self):
        errors = validate_schema_version_for_doc("report-board.v2", "report")
        self.assertEqual(errors["schemaVersion"]["code"], "schema_too_new")

    def test_missing_schema_version_rejected_not_silently_passed(self):
        for doc_type in ("task-card", "report"):
            errors = validate_schema_version_for_doc(None, doc_type)
            self.assertEqual(errors["schemaVersion"]["code"], "identity_missing")

    def test_unknown_old_version_fail_closed(self):
        errors = validate_schema_version_for_doc("task-card-board.v0", "task-card")
        self.assertEqual(errors["schemaVersion"]["code"], "schema_doc_mismatch")

    def test_unknown_doc_type_raises(self):
        with self.assertRaises(ValueError):
            validate_schema_version_for_doc("task-card-board.v1", "event")


class TaskCardDocumentTests(unittest.TestCase):
    """R1：任务卡来源头整体校验。"""

    def test_positive_dispatched_card_passes(self):
        card = task_card_doc(attemptId="att-1", adapter="codex-adapter")
        self.assertEqual(validate_task_card_document(card, dispatched=True), {})

    def test_draft_card_may_omit_attempt_and_adapter(self):
        errors = validate_task_card_document(task_card_doc(), dispatched=False)
        self.assertEqual(errors, {})

    def test_dispatched_card_missing_attempt_id(self):
        errors = validate_task_card_document(task_card_doc(), dispatched=True)
        self.assertEqual(errors["attemptId"]["code"], "identity_missing")

    def test_dispatched_card_missing_adapter(self):
        errors = validate_task_card_document(
            task_card_doc(attemptId="att-1"), dispatched=True
        )
        self.assertEqual(errors["adapter"]["code"], "identity_missing")

    def test_executor_missing_environment_id_rejected(self):
        card = task_card_doc()
        card["executor"] = {
            "alias": "dev-wkb1", "role": "skill-worker",
            "host": "WorkBuddy", "agentId": "dev-wkb1",
        }
        errors = validate_task_card_document(card)
        self.assertEqual(errors["executor.environmentId"]["code"], "identity_missing")

    def test_executor_role_out_of_range_rejected(self):
        card = task_card_doc()
        card["executor"] = dict(card["executor"], role="qa-reporter")
        errors = validate_task_card_document(card)
        self.assertEqual(errors["executor.role"]["code"], "identity_conflict")

    def test_issuer_missing_agent_id_rejected(self):
        card = task_card_doc(issuer={"alias": "cox", "role": "sales-architect", "host": "Codex"})
        errors = validate_task_card_document(card)
        self.assertEqual(errors["issuer.agentId"]["code"], "identity_missing")

    def test_reviewer_missing_alias_rejected(self):
        card = task_card_doc(reviewer={"role": "sales-architect"})
        errors = validate_task_card_document(card)
        self.assertEqual(errors["reviewer.alias"]["code"], "identity_missing")

    def test_session_mode_resume_rejected(self):
        errors = validate_task_card_document(task_card_doc(sessionMode="resume"))
        self.assertEqual(errors["sessionMode"]["code"], "session_mode_unsupported")

    def test_session_mode_missing_rejected(self):
        errors = validate_task_card_document(task_card_doc(sessionMode=None))
        self.assertEqual(errors["sessionMode"]["code"], "identity_missing")

    def test_card_revision_must_be_positive_int(self):
        for bad in (0, "1", 1.0, True, None):
            errors = validate_task_card_document(task_card_doc(cardRevision=bad))
            self.assertEqual(errors["cardRevision"]["code"], "identity_missing", "bad=%r" % (bad,))

    def test_card_id_missing_rejected(self):
        errors = validate_task_card_document(task_card_doc(cardId=""))
        self.assertEqual(errors["cardId"]["code"], "identity_missing")

    def test_board_summary_uses_task_card_field_set(self):
        # 报告字段集（stage/result/...）放进任务卡必须被拒：
        # result 是未冻结字段，title 缺失
        card = task_card_doc(boardSummary=report_board())
        errors = validate_task_card_document(card)
        self.assertEqual(errors["boardSummary.unknown"]["code"], "board_summary_unknown_field")
        self.assertEqual(errors["boardSummary.title"]["code"], "board_summary_missing")

    def test_schema_version_missing_rejected_for_new_card(self):
        errors = validate_task_card_document(task_card_doc(schemaVersion=None))
        self.assertEqual(errors["schemaVersion"]["code"], "identity_missing")

    def test_cross_schema_version_rejected(self):
        errors = validate_task_card_document(task_card_doc(schemaVersion="report-board.v1"))
        self.assertEqual(errors["schemaVersion"]["code"], "schema_doc_mismatch")

    def test_issuer_and_executor_still_validated_separately(self):
        # 发起人不能顶替执行人：executor 缺失时整体校验必须失败
        errors = validate_task_card_document(task_card_doc(executor=None))
        self.assertIn("executor", errors)

    def test_legacy_object_without_schema_version_not_silently_passed(self):
        legacy = {"projectId": "conductor", "taskId": "t-1", "summary": "旧格式交付"}
        errors = validate_task_card_document(legacy)
        self.assertEqual(errors["schemaVersion"]["code"], "identity_missing")
        # 兼容路径仍然只标 pending，不伪造
        _normalized, pending = normalize_legacy_object(legacy)
        self.assertEqual(pending, ["boardSummary", "businessSummary"])


class TaskCardTitleTests(unittest.TestCase):
    """R2：任务卡顶层 title 必填校验（非看板摘要字段）。"""

    def test_valid_title_passes_with_board_title(self):
        # 顶层 title 与 boardSummary.title 同时合法时通过
        card = task_card_doc()
        self.assertEqual(validate_task_card_document(card), {})

    def test_missing_top_level_title_rejected(self):
        card = task_card_doc(title=None)
        errors = validate_task_card_document(card)
        self.assertEqual(errors["title"]["code"], "identity_missing")

    def test_empty_title_rejected(self):
        card = task_card_doc(title="")
        errors = validate_task_card_document(card)
        self.assertEqual(errors["title"]["code"], "identity_missing")

    def test_whitespace_only_title_rejected(self):
        card = task_card_doc(title="   ")
        errors = validate_task_card_document(card)
        self.assertEqual(errors["title"]["code"], "identity_missing")

    def test_non_string_title_rejected(self):
        for bad in (1, ["标题"], {"t": 1}, True, None):
            card = task_card_doc(title=bad)
            errors = validate_task_card_document(card)
            self.assertEqual(errors["title"]["code"], "identity_missing", "bad=%r" % (bad,))

    def test_too_short_title_after_strip_rejected(self):
        # 去除首尾空白后长度至少 2 字符
        card = task_card_doc(title=" a ")
        errors = validate_task_card_document(card)
        self.assertEqual(errors["title"]["code"], "identity_missing")

    def test_title_key_removed_entirely_rejected(self):
        card = task_card_doc()
        del card["title"]
        errors = validate_task_card_document(card)
        self.assertEqual(errors["title"]["code"], "identity_missing")

    def test_top_title_rules_independent_of_board_summary_title(self):
        # boardSummary.title 缺失走 board_summary_missing；顶层 title 合法时不产生 title 错误
        card = task_card_doc(boardSummary={"stage": "阶段", "problem": "暂无", "nextStep": "下一步"})
        errors = validate_task_card_document(card)
        self.assertEqual(errors["boardSummary.title"]["code"], "board_summary_missing")
        self.assertNotIn("title", errors)

    def test_board_summary_title_still_checked_for_tech_leak_and_length(self):
        # boardSummary.title 原有技术词、长度规则不受 R2 影响
        leaky = validate_task_card_document(task_card_doc(boardSummary=task_card_board(nextStep="先跑 d4ctl 再说")))
        self.assertEqual(leaky["boardSummary.nextStep"]["code"], "board_summary_tech_leak")
        long_title = validate_task_card_document(task_card_doc(boardSummary=task_card_board(title="长" * 200)))
        self.assertEqual(long_title["boardSummary.title"]["code"], "board_summary_too_long")


class ReportDocumentTests(unittest.TestCase):
    """R1：报告整体校验（报告头 + 运行事实 + 三层内容）。"""

    def test_positive_report_passes(self):
        self.assertEqual(validate_report_document(full_report_doc()), {})

    def test_report_missing_attempt_id_rejected(self):
        errors = validate_report_document(full_report_doc(attemptId=None))
        self.assertEqual(errors["attemptId"]["code"], "identity_missing")

    def test_report_missing_adapter_rejected(self):
        errors = validate_report_document(full_report_doc(adapter=None))
        self.assertEqual(errors["adapter"]["code"], "identity_conflict")

    def test_report_empty_adapter_rejected(self):
        errors = validate_report_document(full_report_doc(adapter="  "))
        self.assertEqual(errors["adapter"]["code"], "identity_conflict")

    def test_report_unknown_run_facts_must_say_unverified(self):
        errors = validate_report_document(full_report_doc(sessionId=None, hostRunId=""))
        self.assertEqual(errors["sessionId"]["code"], "identity_conflict")
        self.assertEqual(errors["hostRunId"]["code"], "identity_conflict")
        # 显式写 unverified 则放行
        ok = validate_report_document(full_report_doc(sessionId="unverified", hostRunId="unverified"))
        self.assertEqual(ok, {})

    def test_report_missing_reviewed_by_rejected(self):
        errors = validate_report_document(full_report_doc(reviewedBy=None))
        self.assertEqual(errors["reviewedBy"]["code"], "identity_missing")

    def test_report_missing_business_summary_rejected(self):
        errors = validate_report_document(full_report_doc(businessSummary=None))
        self.assertEqual(errors["businessSummary"]["code"], "business_summary_invalid")

    def test_report_missing_evidence_rejected(self):
        errors = validate_report_document(full_report_doc(evidence=None))
        self.assertEqual(errors["evidence"]["code"], "evidence_invalid")

    def test_report_missing_board_summary_rejected(self):
        errors = validate_report_document(full_report_doc(boardSummary=None))
        self.assertIn("boardSummary", errors)

    def test_report_board_summary_uses_report_field_set(self):
        # 任务卡字段集（title/...）放进报告必须被拒：title 未冻结、result 缺失
        report = full_report_doc(boardSummary=task_card_board())
        errors = validate_report_document(report)
        self.assertEqual(errors["boardSummary.unknown"]["code"], "board_summary_unknown_field")
        self.assertEqual(errors["boardSummary.result"]["code"], "board_summary_missing")

    def test_report_cross_schema_version_rejected(self):
        errors = validate_report_document(full_report_doc(schemaVersion="task-card-board.v1"))
        self.assertEqual(errors["schemaVersion"]["code"], "schema_doc_mismatch")

    def test_report_schema_too_new_rejected(self):
        errors = validate_report_document(full_report_doc(schemaVersion="report-board.v2"))
        self.assertEqual(errors["schemaVersion"]["code"], "schema_too_new")

    def test_report_missing_attempt_id_in_header_still_rejected(self):
        # 兼容旧入口：只有报告头时缺 attemptId 也必须被整体校验拦下
        report = full_report_doc()
        del report["attemptId"]
        errors = validate_report_document(report)
        self.assertEqual(errors["attemptId"]["code"], "identity_missing")

    def test_report_with_evidence_item_missing_level_rejected(self):
        report = full_report_doc(evidence=[{"kind": "test", "detail": "x"}])
        errors = validate_report_document(report)
        self.assertEqual(errors["evidence[0]"]["code"], "evidence_invalid")

    def test_report_business_summary_cannot_replace_evidence(self):
        report = full_report_doc(evidence=None, businessSummary={
            "summary": "已完成", "owner": "x", "unfinished": [], "impact": [], "nextStep": "y",
        })
        errors = validate_report_document(report)
        self.assertEqual(errors["evidence"]["code"], "evidence_invalid")

    def test_report_legacy_object_not_silently_passed(self):
        legacy = {"projectId": "conductor", "taskId": "t-1", "summary": "旧格式交付"}
        errors = validate_report_document(legacy)
        self.assertEqual(errors["schemaVersion"]["code"], "identity_missing")


class ErrorCodeStabilityTests(unittest.TestCase):
    def test_all_negative_cases_have_stable_codes(self):
        for code in (
            "board_summary_missing", "board_summary_type", "board_summary_empty",
            "board_summary_too_long", "board_summary_unknown_field", "board_summary_tech_leak",
            "identity_missing", "identity_conflict", "duplicate_key", "source_card_mismatch",
            "schema_too_new", "session_mode_unsupported", "report_missing_executor",
            "business_summary_invalid", "evidence_invalid", "summary_pending",
            "schema_doc_mismatch",
        ):
            self.assertIn(code, BOARD_ERROR_CODES)

    def test_board_contract_error_rejects_unknown_code(self):
        with self.assertRaises(ValueError):
            BoardContractError("not_a_code")

    def test_error_to_dict_shape(self):
        err = BoardContractError("board_summary_missing", details={"fields": ["title"]})
        d = err.to_dict()
        self.assertEqual(d["code"], "board_summary_missing")
        self.assertIn("message", d)
        self.assertEqual(d["details"], {"fields": ["title"]})


if __name__ == "__main__":
    unittest.main()
