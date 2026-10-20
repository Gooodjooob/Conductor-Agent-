"""D4-NEXT-01A：任务卡与回执的双层字段合同（P2A-1）。

对应接口文档《D4 接口与数据约定》第 9.3 / 10.2 节与开发计划 P2A-1：
- 任务卡看板摘要 task-card-board.v1（title/stage/problem/nextStep）
- 回执看板摘要 report-board.v1（stage/result/problem/nextStep）
- 三层内容职责：boardSummary（看板用户）/ businessSummary（CoX/系统）/ evidence（技术证据）

本模块只做结构校验、字段矩阵常量和稳定错误定义，不含业务规则
（状态机、去重、权限、宿主派发在 service/），也不实现 Markdown/YAML 转换器。
所有校验 fail closed：返回错误清单，不写任何乐观状态。
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# 版本与常量
# ---------------------------------------------------------------------------

TASK_CARD_BOARD_V1 = "task-card-board.v1"
REPORT_BOARD_V1 = "report-board.v1"

# boardSummary 字段（冻结；字段集不得增删，只可扩展示例）
TASK_CARD_BOARD_FIELDS = ("title", "stage", "problem", "nextStep")
REPORT_BOARD_FIELDS = ("stage", "result", "problem", "nextStep")

# businessSummary / evidence 的最小结构键
BUSINESS_SUMMARY_KEYS = ("summary", "owner", "unfinished", "impact", "nextStep")
EVIDENCE_ITEM_KEYS = ("kind", "detail", "level")

# evidence.kind 词表（候选；kind 冻结为受控枚举，自由文本放 detail）
EVIDENCE_KINDS = ("command", "file", "test", "raw-error", "other")

# evidence.level 证据级别词表
EVIDENCE_LEVELS = ("verified", "reported", "unverified")

# 回执来源头必填键（qa-report-template.md 来源头）
REPORT_HEADER_REQUIRED = (
    "reportId",
    "reportRevision",
    "reportType",
    "projectId",
    "taskId",
    "sourceCard",
    "requestedBy",
    "executedBy",
)
REPORT_TYPES = ("qa", "qa-regression", "qa-rework")

# sessionMode：MVP 只允许 new；resume 必须明确拒绝
SESSION_MODE_NEW = "new"
SESSION_MODES_SUPPORTED = (SESSION_MODE_NEW,)

# D4-NEXT-01A-R1：schema 版本与文档类型绑定（任务卡/报告各自只认自己的 v1）
SCHEMA_DOC_TYPES = ("task-card", "report")
TASK_CARD_SCHEMA_VERSIONS = (TASK_CARD_BOARD_V1,)
REPORT_SCHEMA_VERSIONS = (REPORT_BOARD_V1,)

# 运行事实占位：真实运行信息未知时必须显式写 unverified，不得留空冒充已知
UNVERIFIED_PLACEHOLDER = "unverified"

# 任务卡身份角色允许范围（D4-NEXT-01A-R1：role 必须有类型与范围约束）
TASK_CARD_ROLES = {
    "issuer": ("sales-architect",),
    "executor": ("skill-worker",),
    "reviewer": ("sales-architect",),
}

# 摘要长度上限（看板一行可读；超出视为违反职责而非截断）
BOARD_FIELD_MAX_CHARS = 120
BOARD_FIELD_MIN_CHARS = 2

# 看板摘要禁止出现的技术词泄漏模式（稳定正则，非语义判断）：
# - 错误码风格：snake_case 词组中含 error/code/invalid/denied 等，如 invalid_event
# - 接口/路径风格：/v1/...、含 .py/.json/.md 扩展名、绝对路径 /Users|/home
# - 命令行风格：以常见命令开头的片段（d4ctl、python、node、npm、git、sqlite）
# - 内部编号风格：evt-/att-/del-/qclaw- 前缀编号
_TECH_LEAK_PATTERNS = (
    # 两种错误码形态：invalid_xxx / xxx_error，取自 D4 ERROR_CODES + BOARD_ERROR_CODES 词形
    ("error_code", r"\b(invalid|duplicate|unknown|stale|permission|submit|host|review|event|board|session|schema|task)[a-z]*_[a-z]+\b|\b[a-z]+_(error|failed|denied|conflict|mismatch|unconfirmed|missing|unsupported)\b"),
    ("api_path", r"/v[0-9]+/|\.py\b|\.json\b|\.yaml\b|/Users/|/home/"),
    ("command_line", r"\b(d4ctl|python3?|node|npm|git|sqlite3?|pip)\b"),
    ("internal_id", r"\b(evt|att|del|qclaw|run)-[0-9a-z-]+\b"),
)

# ---------------------------------------------------------------------------
# 稳定错误码（沿用 ERROR_CODES 风格；本卡新增码集中在 board 层）
# ---------------------------------------------------------------------------

BOARD_ERROR_CODES = {
    "board_summary_missing": "缺少看板摘要或摘要字段，拒绝并指出缺失字段",
    "board_summary_type": "看板摘要字段类型错误，必须是普通中文字符串",
    "board_summary_empty": "看板摘要字段为空或过短，无法让人读懂",
    "board_summary_too_long": "看板摘要字段过长，超出看板一行可读上限",
    "board_summary_unknown_field": "看板摘要出现未冻结字段，拒绝以避免展示歧义",
    "board_summary_tech_leak": "看板摘要泄漏技术词（接口名/错误码/路径/命令/内部编号）",
    "identity_missing": "来源头缺少身份字段，拒绝并指出缺失项",
    "identity_conflict": "身份字段冲突（alias 与 agentId 指向不一致或互相代替）",
    "duplicate_key": "结构化文件头出现重复键，拒绝并保留冲突记录",
    "source_card_mismatch": "报告引用的来源任务卡不存在或不匹配",
    "schema_too_new": "文件 schemaVersion 比当前支持的更新，进入版本协商，不静默解析",
    "session_mode_unsupported": "会话模式未支持（resume 尚未开放），明确拒绝并建议 new",
    "report_missing_executor": "报告缺少实际执行成员（executedBy），拒绝把发起人当执行者",
    "business_summary_invalid": "业务摘要结构不完整或含未定义键",
    "evidence_invalid": "证据条目缺少类型、内容或证据级别",
    "summary_pending": "旧对象缺少新增摘要，标记待补充，不伪造摘要",
    # D4-NEXT-01A-R1：schema 版本与文档类型交叉使用（任务卡/报告版本互串）
    "schema_doc_mismatch": "schemaVersion 与文档类型不匹配（任务卡/报告版本交叉使用），明确拒绝",
}


class BoardContractError(ValueError):
    """带错误码的双层合同错误；调用方遇此异常必须 fail closed。"""

    def __init__(self, code, message=None, details=None):
        if code not in BOARD_ERROR_CODES:
            raise ValueError("unknown board error code: %r" % (code,))
        self.code = code
        self.details = details or {}
        super().__init__(message or BOARD_ERROR_CODES[code])

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _present(value):
    return value not in (None, "", [], {})


def _is_plain_str(value):
    return isinstance(value, str) and value.strip()


# ---------------------------------------------------------------------------
# boardSummary 校验
# ---------------------------------------------------------------------------

def detect_tech_leak(text):
    """检查看板摘要是否泄漏技术词；返回 (leak_kind, matched_fragment) 或 None。"""
    if not isinstance(text, str):
        return None
    for kind, pattern in _TECH_LEAK_PATTERNS:
        import re

        match = re.search(pattern, text)
        if match:
            return kind, match.group(0)
    return None


def validate_board_summary(summary, fields, kind="task-card"):
    """校验一份看板摘要；fields 冻结字段集，kind 用于错误消息。

    返回 {字段或键: 原因}；空字典表示通过。fail closed，不自动修复。
    """
    if not isinstance(summary, dict):
        return {"boardSummary": "%s 的 boardSummary 必须是对象" % kind}
    errors = {}

    if set(summary.keys()) - set(fields):
        unknown = sorted(set(summary.keys()) - set(fields))
        errors["boardSummary.unknown"] = {
            "code": "board_summary_unknown_field",
            "fields": unknown,
        }

    for field in fields:
        if field not in summary:
            errors["boardSummary.%s" % field] = {
                "code": "board_summary_missing",
            }
            continue
        value = summary[field]
        if not isinstance(value, str):
            errors["boardSummary.%s" % field] = {"code": "board_summary_type"}
            continue
        stripped = value.strip()
        if len(stripped) < BOARD_FIELD_MIN_CHARS:
            errors["boardSummary.%s" % field] = {"code": "board_summary_empty"}
            continue
        if len(stripped) > BOARD_FIELD_MAX_CHARS:
            errors["boardSummary.%s" % field] = {
                "code": "board_summary_too_long",
                "limit": BOARD_FIELD_MAX_CHARS,
            }
            continue
        leak = detect_tech_leak(stripped)
        if leak:
            errors["boardSummary.%s" % field] = {
                "code": "board_summary_tech_leak",
                "leak_kind": leak[0],
                "fragment": leak[1],
            }
    return errors


def validate_task_card_board_summary(summary):
    """任务卡看板摘要：title/stage/problem/nextStep（task-card-board.v1）。"""
    return validate_board_summary(summary, TASK_CARD_BOARD_FIELDS, kind="task-card")


def validate_report_board_summary(summary):
    """回执/QA 报告看板摘要：stage/result/problem/nextStep（report-board.v1）。"""
    return validate_board_summary(summary, REPORT_BOARD_FIELDS, kind="report")


# ---------------------------------------------------------------------------
# 三层内容职责校验
# ---------------------------------------------------------------------------

def validate_business_summary(summary):
    """businessSummary：面向 CoX/系统的结构化业务结论。

    summary/owner/nextStep 必填；unfinished/impact 可为空数组但键必须存在。
    允许未来扩展键（前缀保留 ext.），未知裸键拒绝。
    """
    if not isinstance(summary, dict):
        return {"businessSummary": "必须是对象"}
    errors = {}
    for key in ("summary", "owner", "nextStep"):
        if not _present(summary.get(key)):
            errors["businessSummary.%s" % key] = {"code": "business_summary_invalid"}
    for key in ("unfinished", "impact"):
        value = summary.get(key)
        if value is None:
            errors["businessSummary.%s" % key] = {"code": "business_summary_invalid"}
        elif not isinstance(value, list):
            errors["businessSummary.%s" % key] = {"code": "business_summary_invalid"}
    for key in summary:
        if key not in BUSINESS_SUMMARY_KEYS and not key.startswith("ext."):
            errors["businessSummary.%s" % key] = {"code": "business_summary_invalid"}
    return errors


def validate_evidence(evidence):
    """evidence：技术证据条目数组；每条含 kind/detail/level。

    evidence 不能用 businessSummary 代替；kind/level 走受控词表。
    """
    if not isinstance(evidence, list):
        return {"evidence": "必须是数组"}
    errors = {}
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            errors["evidence[%d]" % index] = {"code": "evidence_invalid"}
            continue
        missing = [k for k in EVIDENCE_ITEM_KEYS if k not in item]
        if missing:
            errors["evidence[%d]" % index] = {
                "code": "evidence_invalid",
                "missing": missing,
            }
            continue
        if item.get("kind") not in EVIDENCE_KINDS:
            errors["evidence[%d].kind" % index] = {"code": "evidence_invalid"}
        if item.get("level") not in EVIDENCE_LEVELS:
            errors["evidence[%d].level" % index] = {"code": "evidence_invalid"}
        if not _present(item.get("detail")):
            errors["evidence[%d].detail" % index] = {"code": "evidence_invalid"}
    return errors


# ---------------------------------------------------------------------------
# 身份与来源规则
# ---------------------------------------------------------------------------

def validate_identity_block(block, label):
    """校验 issuer/executor/reviewer/requestedBy/executedBy 身份块。

    - alias 可读名（如 dev-dsh-1）；agentId/host/environmentId 是绑定依据
    - agentId 与 alias 不可互相代替；executor/executedBy 必须有 agentId
    - 未知运行事实（sessionId/hostRunId）必须写 unverified，不得猜值
    """
    if not isinstance(block, dict):
        return {label: "%s 必须是对象" % label}
    errors = {}
    if not _present(block.get("alias")):
        errors["%s.alias" % label] = {"code": "identity_missing"}
    if not _present(block.get("agentId")):
        errors["%s.agentId" % label] = {"code": "identity_missing"}
    if not _present(block.get("host")):
        errors["%s.host" % label] = {"code": "identity_missing"}
    for run_key in ("sessionId", "hostRunId"):
        value = block.get(run_key)
        if value is not None and not isinstance(value, str):
            errors["%s.%s" % (label, run_key)] = {"code": "identity_conflict"}
    return errors


def validate_report_header(header):
    """回执/QA 报告来源头校验。

    - requestedBy（谁请求）与 executedBy（谁实际写报告）分开，不可互相代替
    - executedBy 必须有 environmentId；报告缺实际执行成员直接拒绝
    - sourceCard 必须含 path/cardId/cardRevision
    """
    if not isinstance(header, dict):
        return {"reportHeader": "来源头必须是对象"}
    errors = {}
    missing = [k for k in REPORT_HEADER_REQUIRED if k not in header]
    if missing:
        errors["reportHeader.missing"] = {"code": "identity_missing", "fields": missing}

    report_type = header.get("reportType")
    if _present(report_type) and report_type not in REPORT_TYPES:
        errors["reportHeader.reportType"] = {"code": "identity_conflict"}

    source_card = header.get("sourceCard")
    if not isinstance(source_card, dict):
        errors["reportHeader.sourceCard"] = {"code": "source_card_mismatch"}
    else:
        for key in ("path", "cardId", "cardRevision"):
            if not _present(source_card.get(key)):
                errors["reportHeader.sourceCard.%s" % key] = {
                    "code": "source_card_mismatch"
                }

    requested_by = header.get("requestedBy")
    executed_by = header.get("executedBy")
    for label, block in (("requestedBy", requested_by), ("executedBy", executed_by)):
        for key, value in validate_identity_block(block, label).items():
            errors[key] = value
    # executedBy 必须绑定环境；发起人不能代替执行者
    if isinstance(executed_by, dict) and not _present(executed_by.get("environmentId")):
        errors["executedBy.environmentId"] = {"code": "report_missing_executor"}
    if (
        isinstance(requested_by, dict)
        and isinstance(executed_by, dict)
        and _present(requested_by.get("agentId"))
        and _present(executed_by.get("agentId"))
        and requested_by.get("agentId") == executed_by.get("agentId")
        and _present(requested_by.get("role"))
        and requested_by.get("role") != executed_by.get("role")
    ):
        # 同一 agentId 声明两种角色且无解释键时视为身份冲突（role 不同即冲突）
        errors["requestedBy.agentId"] = {"code": "identity_conflict"}

    return errors


# ---------------------------------------------------------------------------
# 会话模式与版本协商
# ---------------------------------------------------------------------------

def validate_session_mode(session_mode):
    """sessionMode：MVP 只允许 new；resume 明确拒绝并说明原因。"""
    if not _present(session_mode):
        return {"sessionMode": {"code": "identity_missing"}}
    if session_mode in SESSION_MODES_SUPPORTED:
        return {}
    if session_mode == "resume":
        return {
            "sessionMode": {
                "code": "session_mode_unsupported",
                "reason": "resume 尚未开放；每轮默认新会话，续用另卡验证",
            }
        }
    return {"sessionMode": {"code": "identity_conflict"}}


def validate_schema_version(schema_version, supported=(TASK_CARD_BOARD_V1, REPORT_BOARD_V1)):
    """版本协商：过新版本拒绝解析（schema_too_new），旧版本走兼容规则。"""
    if not _present(schema_version):
        return {"schemaVersion": {"code": "identity_missing"}}
    if schema_version in supported:
        return {}
    if str(schema_version).endswith(".v2") or str(schema_version) > max(supported):
        return {
            "schemaVersion": {
                "code": "schema_too_new",
                "supported": list(supported),
            }
        }
    return {}


# ---------------------------------------------------------------------------
# D4-NEXT-01A-R1：schema 版本绑定文档类型 + 任务卡/报告整体校验
# 说明：只新增，不改既有函数语义；旧函数（validate_schema_version 等）保持原样。
# ---------------------------------------------------------------------------

def validate_role_range(block, label, allowed):
    """身份块 role 的类型与允许范围校验（R1 新增）。

    - role 缺失时此处不报（identity_missing 由 validate_identity_block 上报）；
    - role 非字符串或超出 allowed 词表 → identity_conflict。
    """
    if not isinstance(block, dict):
        return {}
    role = block.get("role")
    if not _present(role):
        return {}
    if not isinstance(role, str) or role not in allowed:
        return {"%s.role" % label: {"code": "identity_conflict", "allowed": list(allowed)}}
    return {}


def _validate_reviewer_block(block, label):
    """reviewer/reviewedBy 校验（R1 新增）。

    矩阵 §11.1/§11.2 对 reviewer 只冻结 alias/role 必填；agentId/host
    可选，出现时必须是字符串。
    """
    if not isinstance(block, dict):
        return {label: {"code": "identity_missing", "reason": "%s 必须是对象" % label}}
    errors = {}
    if not _present(block.get("alias")):
        errors["%s.alias" % label] = {"code": "identity_missing"}
    for key in ("role",):
        if not _present(block.get(key)):
            errors["%s.%s" % (label, key)] = {"code": "identity_missing"}
    for key in ("agentId", "host"):
        value = block.get(key)
        if value is not None and not _is_plain_str(value):
            errors["%s.%s" % (label, key)] = {"code": "identity_conflict"}
    return errors


def validate_schema_version_for_doc(schema_version, doc_type):
    """按文档类型绑定 schema 版本（R1 新增）。

    - 任务卡只接受 task-card-board.v1；报告只接受 report-board.v1；
    - 交叉使用（把报告版本放进任务卡或反之）→ schema_doc_mismatch；
    - 比当前支持版本更新 → schema_too_new，进入版本协商；
    - 缺 schemaVersion → identity_missing（旧对象只能走 normalize_legacy_object
      兼容路径，不得被新版整体校验默默放行）。
    """
    if doc_type not in SCHEMA_DOC_TYPES:
        raise ValueError("unknown doc type: %r" % (doc_type,))
    supported = TASK_CARD_SCHEMA_VERSIONS if doc_type == "task-card" else REPORT_SCHEMA_VERSIONS
    if not _present(schema_version):
        return {"schemaVersion": {"code": "identity_missing"}}
    if schema_version in supported:
        return {}
    other = REPORT_SCHEMA_VERSIONS if doc_type == "task-card" else TASK_CARD_SCHEMA_VERSIONS
    if schema_version in other:
        return {
            "schemaVersion": {
                "code": "schema_doc_mismatch",
                "docType": doc_type,
                "expected": list(supported),
                "found": schema_version,
            }
        }
    if str(schema_version) > max(supported):
        return {
            "schemaVersion": {
                "code": "schema_too_new",
                "supported": list(supported),
            }
        }
    # 未知版本（既不是当前版本、不是另一文档类型的当前版本、也不比当前新）
    # 一律 fail closed，不静默解析。
    return {
        "schemaVersion": {
            "code": "schema_doc_mismatch",
            "docType": doc_type,
            "expected": list(supported),
            "found": schema_version,
        }
    }


def validate_task_card_document(card, dispatched=False):
    """任务卡整体校验（R1 新增）：来源头 + 会话模式 + 看板摘要。

    - dispatched=True（已派发卡）时 attemptId/adapter 必填；
      草稿卡（dispatched=False）按现有规则允许留空；
    - issuer/executor 走完整身份块校验；reviewer 按矩阵只要求 alias/role；
    - executor.environmentId 必须存在且为非空字符串；
    - issuer 与 executor 分开校验，发起人不能顶替执行人；
    - 返回 {路径: 原因}；空字典表示通过。fail closed，不自动修复。
    """
    if not isinstance(card, dict):
        return {"taskCard": {"code": "identity_missing", "reason": "任务卡必须是对象"}}
    errors = {}

    errors.update(validate_schema_version_for_doc(card.get("schemaVersion"), "task-card"))

    for key in ("cardId", "projectId"):
        if not _is_plain_str(card.get(key)):
            errors[key] = {"code": "identity_missing"}
    revision = card.get("cardRevision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        errors["cardRevision"] = {"code": "identity_missing"}

    issuer = card.get("issuer")
    if not isinstance(issuer, dict):
        errors["issuer"] = {"code": "identity_missing", "reason": "issuer 必须是对象"}
    else:
        errors.update(validate_identity_block(issuer, "issuer"))
        errors.update(validate_role_range(issuer, "issuer", TASK_CARD_ROLES["issuer"]))

    executor = card.get("executor")
    if not isinstance(executor, dict):
        errors["executor"] = {"code": "identity_missing", "reason": "executor 必须是对象"}
    else:
        errors.update(validate_identity_block(executor, "executor"))
        errors.update(validate_role_range(executor, "executor", TASK_CARD_ROLES["executor"]))
        # 稳定环境绑定：缺 environmentId 或空值一律身份错误
        if not _is_plain_str(executor.get("environmentId")):
            errors["executor.environmentId"] = {"code": "identity_missing"}

    reviewer = card.get("reviewer")
    if not isinstance(reviewer, dict):
        errors["reviewer"] = {"code": "identity_missing", "reason": "reviewer 必须是对象"}
    else:
        errors.update(_validate_reviewer_block(reviewer, "reviewer"))
        errors.update(validate_role_range(reviewer, "reviewer", TASK_CARD_ROLES["reviewer"]))

    source_task_id = card.get("sourceTaskId")
    if source_task_id is not None and not _is_plain_str(source_task_id):
        errors["sourceTaskId"] = {"code": "identity_missing"}

    # sessionMode 必填；resume 返回 session_mode_unsupported
    if _present(card.get("sessionMode")):
        errors.update(validate_session_mode(card.get("sessionMode")))
    else:
        errors["sessionMode"] = {"code": "identity_missing"}

    # 已派发卡的执行轮次与投递适配器必填
    if dispatched:
        for key in ("attemptId", "adapter"):
            if not _is_plain_str(card.get(key)):
                errors[key] = {"code": "identity_missing"}

    # 顶层 title（D4-NEXT-01A-R2）：任务名属顶层任务元数据，缺失或无效
    # 用 identity_missing（与 cardId/projectId 同口径）；规则同看板长度下限
    top_title = card.get("title")
    if not isinstance(top_title, str) or len(top_title.strip()) < BOARD_FIELD_MIN_CHARS:
        errors["title"] = {
            "code": "identity_missing",
            "reason": "顶层 title 必须是非空字符串，去除首尾空白后至少 %d 个字符" % BOARD_FIELD_MIN_CHARS,
        }

    # boardSummary 使用任务卡字段集 title/stage/problem/nextStep
    errors.update(validate_task_card_board_summary(card.get("boardSummary")))
    return errors


def validate_report_document(report):
    """回执/QA 报告整体校验（R1 新增）：报告头 + 运行事实 + 三层内容。

    - 在 validate_report_header 基础上补齐 attemptId / adapter / sessionId /
      hostRunId / reviewedBy 必填；adapter/sessionId/hostRunId 未知时必须
      显式写 unverified，不得留空冒充已知；
    - businessSummary / evidence / boardSummary 必须同时存在且各自合法；
    - 返回 {路径: 原因}；空字典表示通过。fail closed，不自动修复。
    """
    if not isinstance(report, dict):
        return {"report": {"code": "identity_missing", "reason": "报告必须是对象"}}
    errors = {}

    errors.update(validate_schema_version_for_doc(report.get("schemaVersion"), "report"))
    errors.update(validate_report_header(report))

    # 运行事实：attemptId 必填；adapter/sessionId/hostRunId 必填，未知写 unverified
    if not _is_plain_str(report.get("attemptId")):
        errors["attemptId"] = {"code": "identity_missing"}
    for key in ("adapter", "sessionId", "hostRunId"):
        value = report.get(key)
        if value != UNVERIFIED_PLACEHOLDER and not _is_plain_str(value):
            errors[key] = {"code": "identity_conflict"}

    reviewed_by = report.get("reviewedBy")
    if not isinstance(reviewed_by, dict):
        errors["reviewedBy"] = {"code": "identity_missing", "reason": "reviewedBy 必须是对象"}
    else:
        errors.update(_validate_reviewer_block(reviewed_by, "reviewedBy"))

    # 三层内容：缺任一层直接拒绝，摘要不能替代证据
    if not _present(report.get("businessSummary")):
        errors["businessSummary"] = {"code": "business_summary_invalid"}
    else:
        errors.update(validate_business_summary(report["businessSummary"]))

    if not _present(report.get("evidence")):
        errors["evidence"] = {"code": "evidence_invalid"}
    else:
        errors.update(validate_evidence(report["evidence"]))

    errors.update(validate_report_board_summary(report.get("boardSummary")))
    return errors




# ---------------------------------------------------------------------------
# 重复键检测（供解析层使用；本卡不实现解析器，只冻结判定规则）
# ---------------------------------------------------------------------------

def detect_duplicate_keys(pairs):
    """给解析层的重复键判定：输入 (key, value) 序列，返回重复键清单。

    YAML/JSON 解析器必须把本函数结果映射为 duplicate_key 错误；
    本函数只做判定，不做修复，不保留后值覆盖先值。
    """
    seen = set()
    duplicates = []
    for key, _value in pairs:
        if key in seen:
            duplicates.append(key)
        seen.add(key)
    return duplicates


# ---------------------------------------------------------------------------
# 旧版 JSON 兼容（冻结规则：待补充，不伪造）
# ---------------------------------------------------------------------------

def normalize_legacy_object(obj):
    """旧版 D4 JSON（无摘要字段）的兼容规则。

    - 旧字段语义不变；
    - 缺少 boardSummary/businessSummary 时标记 pending，不伪造摘要；
    - 返回 (normalized, pending_keys)；pending_keys 非空时看板只能显示
      "待补充"，不得自行生成摘要文本。
    """
    if not isinstance(obj, dict):
        return obj, []
    normalized = dict(obj)
    pending = []
    if not _present(obj.get("boardSummary")):
        pending.append("boardSummary")
    if not _present(obj.get("businessSummary")):
        pending.append("businessSummary")
    return normalized, pending
