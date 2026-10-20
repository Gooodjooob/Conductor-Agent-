"""D4-PROJECT-01：project-intake.v1 / project-update.v1 结构合同（只校验，不写库）。

对应《D4 接口与数据约定》第 12 节与《D4 开发计划》第 8 节：

- ``project-intake.v1``：批次导入资料，固定项目 / 阶段 / 历史任务 / 来源卡 / 问题关系；
- ``project-update.v1``：已登记事项的新进展声明，**不是**运行事件，也不是正式审核。

职责边界（本模块刻意不做的事）：

- 不含状态机、去重落库、并发写事务、权限与宿主派发（那些在 ``service/`` 与后续卡）；
- 不解析 YAML、不读文件、不落任何盘、不触碰数据库；
- 不新增通用 YAML 解析器：两个新格式只走 UTF-8 JSON（接口 §12.2）。

所有校验 fail closed：返回 ``{错误路径: 原因}`` 明细，不写任何乐观状态，也不自动修复。
"""

from __future__ import annotations

import json
import re

from contracts.board_summary import (
    BOARD_ERROR_CODES,
    detect_duplicate_keys,
    detect_tech_leak,
    validate_business_summary,
    validate_evidence,
    validate_report_board_summary,
    validate_task_card_board_summary,
)
from contracts.document_io import DOC_ERROR_CODES
from contracts.schemas import ERROR_CODES

# ---------------------------------------------------------------------------
# 版本与常量
# ---------------------------------------------------------------------------

PROJECT_INTAKE_V1 = "project-intake.v1"
PROJECT_UPDATE_V1 = "project-update.v1"

INTAKE_SCHEMA_VERSIONS = (PROJECT_INTAKE_V1,)
UPDATE_SCHEMA_VERSIONS = (PROJECT_UPDATE_V1,)

# 编号词表：与 projections/project_catalog.py 的 _ID 保持同一形状
ID_PATTERN = r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"
_ID_RE = re.compile(ID_PATTERN + r"\Z")

# 分类与状态是「资料分类」，不是事件状态机（接口 §12.2）
TASK_CATEGORIES = ("implementation", "validation", "research", "planned")
TASK_STATUSES = (
    "verified",
    "validated",
    "partial",
    "in_progress",
    "blocked",
    "planned",
    "awaiting_review",
)
ISSUE_STATUSES = ("open", "deferred", "closed")
UPDATE_KINDS = ("progress", "delivery_report", "problem")

# 旧卡确无版本时的显式占位；不得推测补 1（接口 §12.2）
CARD_REVISION_UNVERIFIED = "unverified"

# ---------------------------------------------------------------------------
# 规模上限（fail closed：超限拒绝并列出，不截断证据）
# 取值口径：JSON 与 catalog 沿用接口 §12.2 已写死的 2 MiB / 512 KiB；
# 集合规模为本卡新定义的保守上限，供 cox 裁定后收敛。
# ---------------------------------------------------------------------------

INTAKE_JSON_MAX_BYTES = 2 * 1024 * 1024
MATERIAL_MAX_BYTES = 512 * 1024
CATALOG_MAX_BYTES = 2 * 1024 * 1024

MAX_PROJECTS = 100
MAX_MILESTONES_PER_PROJECT = 100
MAX_SOURCES_PER_PROJECT = 50
MAX_TASKS_PER_PROJECT = 500
MAX_ISSUES_PER_PROJECT = 200
MAX_CARD_REFS_PER_TASK = 50
MAX_DOCUMENTS_PER_TASK = 100
MAX_HISTORY_PER_TASK = 200
MAX_EVIDENCE_ITEMS = 100

# 更新文件明确不接受的运行 / 授权字段（接口 §12.4）
# 这些事实不能从来源正文、证据 level 或摘要推导出来。
UPDATE_FORBIDDEN_FIELDS = (
    "approved",
    "accepted",
    "attemptId",
    "hostRunId",
    "permissions",
)
# 同族的运行字段一并拒绝：避免用换名的方式绕过上面五项。
UPDATE_FORBIDDEN_ALIASES = (
    "deliveryId",
    "reviewedBy",
    "reviewDecision",
    "sessionId",
    "adapter",
    "sessionMode",
)

# 模板占位符：结构层放行（不算结构错误），但预检必须标为「待补充」，
# 不能算作可应用（任务卡实施步骤 3）。
_REPLACE_PATH_RE = re.compile(r"/REPLACE_[A-Z0-9_]*/")
_PLACEHOLDER_TOKEN_RE = re.compile(r"<[^<>]{1,80}>")

# ---------------------------------------------------------------------------
# 字段矩阵（唯一权威：接口合同 §12.2 / §12.4）
# ---------------------------------------------------------------------------

INTAKE_TOP_REQUIRED = (
    "schemaVersion",
    "intakeId",
    "intakeRevision",
    "preparedAt",
    "preparedBy",
    "projects",
)
INTAKE_TOP_ALLOWED = INTAKE_TOP_REQUIRED

PREPARED_BY_REQUIRED = ("alias", "role", "host", "agentId")

PROJECT_REQUIRED = (
    "projectId",
    "name",
    "goal",
    "owner",
    "workspace",
    "stage",
    "currentWork",
    "nextStep",
    "sources",
    "milestones",
    "tasks",
    "issues",
)
PROJECT_ALLOWED = PROJECT_REQUIRED

SOURCE_REQUIRED = ("path", "description")
SOURCE_ALLOWED = SOURCE_REQUIRED

MILESTONE_REQUIRED = ("id", "title", "summary")
MILESTONE_ALLOWED = MILESTONE_REQUIRED

TASK_REQUIRED = (
    "taskId",
    "title",
    "goal",
    "milestoneId",
    "category",
    "status",
    "member",
    "verification",
    "boardSummary",
    "cardRefs",
    "documents",
    "history",
)
TASK_ALLOWED = TASK_REQUIRED + ("result", "problem", "nextStep", "path", "runtimeTaskId")

CARD_REF_REQUIRED = ("cardId", "cardRevision", "path")
CARD_REF_ALLOWED = CARD_REF_REQUIRED

DOCUMENT_REQUIRED = ("name", "kind", "sourcePath")
DOCUMENT_ALLOWED = DOCUMENT_REQUIRED

HISTORY_REQUIRED = ("title", "note", "documentIndexes")
HISTORY_ALLOWED = HISTORY_REQUIRED

ISSUE_REQUIRED = ("issueId", "taskId", "title", "impact", "owner", "status", "nextStep")
ISSUE_ALLOWED = ISSUE_REQUIRED + ("resolutionEvidence",)

UPDATE_TOP_REQUIRED = (
    "schemaVersion",
    "updateId",
    "updateRevision",
    "projectId",
    "taskId",
    "expectedProjectRevision",
    "kind",
    "occurredAt",
    "reportedBy",
    "sourceCard",
    "sourceReport",
    "boardSummary",
    "businessSummary",
    "evidence",
)
UPDATE_TOP_ALLOWED = UPDATE_TOP_REQUIRED + ("supersedesUpdateId",)

SOURCE_REPORT_REQUIRED = ("reportId", "reportRevision", "path")
SOURCE_REPORT_ALLOWED = SOURCE_REPORT_REQUIRED

# ---------------------------------------------------------------------------
# 稳定错误码
#
# 语义分工（接口 §12.7：错误至少区分结构不合法、目录越界、未知项目/任务、
# 卡版本不符、内容冲突、项目版本陈旧和来源漂移）：
# - 结构/类型/枚举/编号/规模 → 本模块新码；
# - 重复键、摘要噪音、证据、卡关联、版本过新 → 复用 board_summary 既有码，
#   不改变其语义；
# - 相对路径、符号链接、越界 → 复用 document_io / schemas 既有码。
# ---------------------------------------------------------------------------

INTAKE_ERROR_CODES = {
    "intake_structure_invalid": "资料结构不合法（缺必填字段或该层不是对象/数组），拒绝",
    "intake_unknown_field": "出现未冻结的未知字段，拒绝以避免展示歧义",
    "intake_type_invalid": "字段类型错误（含用 bool 冒充整数、用字符串冒充数组）",
    "intake_identifier_invalid": "编号不符合受控格式，拒绝（不自动纠正）",
    "intake_enum_invalid": "分类/状态/类型超出受控词表，拒绝",
    "intake_schema_mismatch": "schemaVersion 与本文件类型交叉使用，明确拒绝",
    "intake_limit_exceeded": "集合规模超出上限，拒绝并列出超限位置",
    "intake_too_large": "JSON 文件超过大小上限，拒绝解析",
    "intake_material_too_large": "来源材料超过单份大小上限，拒绝并列出文件",
    "intake_relation_duplicate": "项目/任务/问题/来源卡唯一关联重复，拒绝",
    "intake_material_index_invalid": "材料索引越界或类型非法，拒绝",
    "intake_card_revision_invalid": "来源卡版本非法（须为正整数或显式 unverified），拒绝",
    "intake_issue_resolution_missing": "已关闭问题缺少 resolutionEvidence 材料索引，拒绝",
    "intake_source_unreadable": "来源不可读（不存在/非普通文件/非 UTF-8），拒绝",
    "intake_source_drift": "来源预检前后字节或摘要变化，本批停止",
    "intake_not_configured": "预检缺少受控配置（允许根/来源上限由服务注入，不接受 payload 指定）",
    "update_run_field_forbidden": "更新文件出现运行或授权字段，拒绝（资料声明不等于运行事实）",
}

# 与 board / schemas / document_io 共用同一命名空间，避免同名不同义
KNOWN_ERROR_CODES = (
    set(INTAKE_ERROR_CODES)
    | set(BOARD_ERROR_CODES)
    | set(ERROR_CODES)
    | set(DOC_ERROR_CODES)
)


class ProjectIntakeContractError(ValueError):
    """项目资料结构/预检错误；调用方遇此异常必须 fail closed。

    继承 ``ValueError`` 以便 CLI 统一兜底，但错误码必须先在本模块词表内，
    不允许临时造码。
    """

    def __init__(self, code, message=None, details=None):
        if code not in KNOWN_ERROR_CODES:
            raise ValueError("unknown project intake error code: %r" % (code,))
        self.code = code
        self.details = details or {}
        super().__init__(
            message
            or INTAKE_ERROR_CODES.get(code)
            or BOARD_ERROR_CODES.get(code)
            or DOC_ERROR_CODES.get(code)
            or ERROR_CODES.get(code)
            or code
        )

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _present(value):
    """值是否算「已提供」；空字符串、空数组、空对象视为未提供。"""
    return value not in (None, "", [], {})


def _is_plain_str(value):
    return isinstance(value, str) and bool(value.strip())


def _is_pos_int(value):
    """正整数；显式拒绝 bool（True/False 会被 isinstance(int) 接住，必须挡掉）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_non_neg_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_id(value):
    return isinstance(value, str) and bool(_ID_RE.match(value))


def _unknown_keys(obj, allowed, path, errors):
    extra = sorted(set(obj.keys()) - set(allowed))
    if extra:
        errors[path] = {"code": "intake_unknown_field", "fields": extra}


def _required_keys(obj, required, path, errors):
    missing = [key for key in required if key not in obj]
    if missing:
        errors[path] = {"code": "intake_structure_invalid", "missing": missing}


def _check_collection(value, path, errors, limit=None, allow_empty=True):
    """集合校验：必须是数组；可选规模上限；可选非空。"""
    if not isinstance(value, list):
        errors[path] = {"code": "intake_type_invalid", "expected": "array"}
        return False
    if not allow_empty and not value:
        errors[path] = {"code": "intake_structure_invalid", "reason": "不允许空数组"}
        return False
    if limit is not None and len(value) > limit:
        errors[path] = {
            "code": "intake_limit_exceeded",
            "count": len(value),
            "limit": limit,
        }
        return False
    return True


def _check_text(value, path, errors, required=True):
    if not _is_plain_str(value):
        if required:
            errors[path] = {"code": "intake_structure_invalid", "reason": "必须是非空字符串"}
        return False
    return True


def _is_placeholder(value):
    """模板占位符（如 /REPLACE_WITH_APPROVED_ROOT/... 或 <路径>）。"""
    if not isinstance(value, str):
        return False
    return bool(_REPLACE_PATH_RE.search(value) or _PLACEHOLDER_TOKEN_RE.search(value))


def _check_abs_path(value, path, errors):
    """绝对路径：允许占位符（结构层放行，预检另标待补充）；拒相对路径与 ``..`` 逃逸。"""
    if not _is_plain_str(value):
        errors[path] = {"code": "intake_structure_invalid", "reason": "路径必填"}
        return
    if _is_placeholder(value):
        return
    if not value.startswith("/"):
        errors[path] = {"code": "path_not_absolute", "path": value}
        return
    if ".." in value.split("/"):
        errors[path] = {"code": "path_escape", "path": value}


def _check_schema_version(obj, supported, other, errors):
    value = obj.get("schemaVersion")
    if not _is_plain_str(value):
        errors["schemaVersion"] = {"code": "identity_missing"}
        return
    if value in supported:
        return
    if value in other:
        errors["schemaVersion"] = {
            "code": "intake_schema_mismatch",
            "expected": list(supported),
            "found": value,
        }
        return
    errors["schemaVersion"] = {"code": "schema_too_new", "supported": list(supported)}


def _check_identity_block(block, label, required, errors):
    """资料整理身份块：``alias/role/host/agentId`` 四键固定。

    这是「谁整理了资料」，不等于受控导入身份或实际执行人，因此不接受
    sessionId / hostRunId 等运行事实键（出现即按未知字段拒绝）。
    """
    if not isinstance(block, dict):
        errors[label] = {"code": "intake_structure_invalid", "reason": "必须是对象"}
        return
    _required_keys(block, required, label, errors)
    _unknown_keys(block, required, label, errors)
    for key in required:
        if key in block and not _is_plain_str(block.get(key)):
            errors["%s.%s" % (label, key)] = {"code": "intake_type_invalid"}


def _check_card_revision(value, path, errors):
    """cardRevision：正整数，或显式字符串 unverified；bool 冒充整数必须拒绝。"""
    if isinstance(value, str):
        if value == CARD_REVISION_UNVERIFIED:
            return
        errors[path] = {
            "code": "intake_card_revision_invalid",
            "reason": "字符串只接受 %r" % (CARD_REVISION_UNVERIFIED,),
            "found": value,
        }
        return
    if not _is_pos_int(value):
        errors[path] = {"code": "intake_card_revision_invalid", "found": value}


def _dedupe(values, kind, errors, path):
    """唯一性：同层内重复即拒绝（不自动去重、不保留后者）。"""
    seen = set()
    duplicates = []
    for value in values:
        if value in seen:
            duplicates.append(value)
        seen.add(value)
    if duplicates:
        errors[path] = {
            "code": "intake_relation_duplicate",
            "kind": kind,
            "duplicates": sorted(set(duplicates)),
        }


# ---------------------------------------------------------------------------
# 批次解析：UTF-8 JSON + 重复键 fail closed
# ---------------------------------------------------------------------------

def _strict_object_pairs(pairs):
    duplicates = detect_duplicate_keys(pairs)
    if duplicates:
        raise ProjectIntakeContractError(
            "duplicate_key",
            message="结构化文件出现重复键，拒绝并保留冲突记录",
            details={"duplicates": sorted(set(duplicates))},
        )
    return dict(pairs)


def parse_intake_json(text):
    """解析 UTF-8 JSON 文本；重复键、非法 JSON 一律 fail closed。

    返回解析后的对象（不做结构校验，交给 validate_* 分派）。
    """
    if not isinstance(text, str):
        raise ProjectIntakeContractError(
            "intake_structure_invalid", message="输入必须是 UTF-8 文本"
        )
    if len(text.encode("utf-8")) > INTAKE_JSON_MAX_BYTES:
        raise ProjectIntakeContractError(
            "intake_too_large",
            details={"limit": INTAKE_JSON_MAX_BYTES, "bytes": len(text.encode("utf-8"))},
        )
    try:
        return json.loads(text, object_pairs_hook=_strict_object_pairs)
    except json.JSONDecodeError as exc:
        raise ProjectIntakeContractError(
            "intake_structure_invalid",
            message="JSON 无法解析，拒绝",
            details={"line": exc.lineno, "column": exc.colno, "reason": exc.msg},
        ) from exc


def detect_schema_version(obj):
    """从对象里判断它要走 intake 还是 update 校验；无法判断时返回 None。"""
    if not isinstance(obj, dict):
        return None
    value = obj.get("schemaVersion")
    if value in INTAKE_SCHEMA_VERSIONS:
        return "intake"
    if value in UPDATE_SCHEMA_VERSIONS:
        return "update"
    return None


# ---------------------------------------------------------------------------
# project-intake.v1 结构校验
# ---------------------------------------------------------------------------

def _validate_source(source, path, errors):
    if not isinstance(source, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "来源必须是对象"}
        return
    _required_keys(source, SOURCE_REQUIRED, path, errors)
    _unknown_keys(source, SOURCE_ALLOWED, path, errors)
    _check_abs_path(source.get("path"), path + ".path", errors)
    _check_text(source.get("description"), path + ".description", errors)


def _validate_milestone(milestone, path, errors):
    if not isinstance(milestone, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "阶段必须是对象"}
        return
    _required_keys(milestone, MILESTONE_REQUIRED, path, errors)
    _unknown_keys(milestone, MILESTONE_ALLOWED, path, errors)
    if not _is_id(milestone.get("id")):
        errors[path + ".id"] = {"code": "intake_identifier_invalid", "found": milestone.get("id")}
    for key in ("title", "summary"):
        _check_text(milestone.get(key), "%s.%s" % (path, key), errors)


def _validate_card_ref(card_ref, path, errors):
    if not isinstance(card_ref, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "来源卡必须是对象"}
        return
    _required_keys(card_ref, CARD_REF_REQUIRED, path, errors)
    _unknown_keys(card_ref, CARD_REF_ALLOWED, path, errors)
    if not _is_id(card_ref.get("cardId")):
        errors[path + ".cardId"] = {"code": "intake_identifier_invalid", "found": card_ref.get("cardId")}
    if "cardRevision" in card_ref:
        _check_card_revision(card_ref.get("cardRevision"), path + ".cardRevision", errors)
    _check_abs_path(card_ref.get("path"), path + ".path", errors)


def _validate_document(document, path, errors):
    if not isinstance(document, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "材料必须是对象"}
        return
    _required_keys(document, DOCUMENT_REQUIRED, path, errors)
    _unknown_keys(document, DOCUMENT_ALLOWED, path, errors)
    for key in ("name", "kind"):
        _check_text(document.get(key), "%s.%s" % (path, key), errors)
    _check_abs_path(document.get("sourcePath"), path + ".sourcePath", errors)


def _validate_history_entry(entry, path, errors, document_count):
    if not isinstance(entry, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "历史必须是对象"}
        return
    _required_keys(entry, HISTORY_REQUIRED, path, errors)
    _unknown_keys(entry, HISTORY_ALLOWED, path, errors)
    for key in ("title", "note"):
        _check_text(entry.get(key), "%s.%s" % (path, key), errors)
    indexes = entry.get("documentIndexes")
    if not isinstance(indexes, list):
        errors[path + ".documentIndexes"] = {"code": "intake_type_invalid", "expected": "array"}
        return
    for position, index in enumerate(indexes):
        index_path = "%s.documentIndexes[%d]" % (path, position)
        if not _is_non_neg_int(index):
            errors[index_path] = {"code": "intake_material_index_invalid", "found": index}
            continue
        if index >= document_count:
            errors[index_path] = {
                "code": "intake_material_index_invalid",
                "found": index,
                "documents": document_count,
            }


def _validate_task(task, path, errors, milestone_ids):
    if not isinstance(task, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "任务必须是对象"}
        return
    _required_keys(task, TASK_REQUIRED, path, errors)
    _unknown_keys(task, TASK_ALLOWED, path, errors)

    if not _is_id(task.get("taskId")):
        errors[path + ".taskId"] = {"code": "intake_identifier_invalid", "found": task.get("taskId")}
    for key in ("title", "goal", "member", "verification"):
        _check_text(task.get(key), "%s.%s" % (path, key), errors)

    if task.get("milestoneId") not in milestone_ids:
        errors[path + ".milestoneId"] = {
            "code": "intake_structure_invalid",
            "reason": "milestoneId 必须指向本项目已声明阶段",
            "found": task.get("milestoneId"),
        }
    if task.get("category") not in TASK_CATEGORIES:
        errors[path + ".category"] = {
            "code": "intake_enum_invalid",
            "allowed": list(TASK_CATEGORIES),
            "found": task.get("category"),
        }
    if task.get("status") not in TASK_STATUSES:
        errors[path + ".status"] = {
            "code": "intake_enum_invalid",
            "allowed": list(TASK_STATUSES),
            "found": task.get("status"),
        }

    errors.update(validate_task_card_board_summary(task.get("boardSummary")))

    documents = task.get("documents")
    document_count = 0
    if _check_collection(documents, path + ".documents", errors,
                         limit=MAX_DOCUMENTS_PER_TASK):
        document_count = len(documents)
        for index, document in enumerate(documents):
            _validate_document(document, "%s.documents[%d]" % (path, index), errors)

    card_refs = task.get("cardRefs")
    if _check_collection(card_refs, path + ".cardRefs", errors, limit=MAX_CARD_REFS_PER_TASK):
        for index, card_ref in enumerate(card_refs):
            _validate_card_ref(card_ref, "%s.cardRefs[%d]" % (path, index), errors)
        _dedupe([ref.get("cardId") for ref in card_refs if isinstance(ref, dict)],
                "cardId", errors, path + ".cardRefs")
        # 已执行事项至少一个来源卡；计划项可空数组（接口 §12.2）
        if task.get("category") != "planned" and not card_refs:
            errors[path + ".cardRefs"] = {
                "code": "intake_structure_invalid",
                "reason": "已执行事项至少需要一个来源卡引用",
            }

    history = task.get("history")
    if _check_collection(history, path + ".history", errors, limit=MAX_HISTORY_PER_TASK):
        for index, entry in enumerate(history):
            _validate_history_entry(
                entry, "%s.history[%d]" % (path, index), errors, document_count
            )

    for key in ("result", "problem", "nextStep", "path", "runtimeTaskId"):
        if key in task and not _is_plain_str(task.get(key)):
            errors["%s.%s" % (path, key)] = {"code": "intake_type_invalid"}


def _validate_issue(issue, path, errors, task_document_counts):
    if not isinstance(issue, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "问题必须是对象"}
        return
    _required_keys(issue, ISSUE_REQUIRED, path, errors)
    _unknown_keys(issue, ISSUE_ALLOWED, path, errors)

    if not _is_id(issue.get("issueId")):
        errors[path + ".issueId"] = {"code": "intake_identifier_invalid", "found": issue.get("issueId")}
    task_id = issue.get("taskId")
    if task_id not in task_document_counts:
        errors[path + ".taskId"] = {
            "code": "intake_structure_invalid",
            "reason": "taskId 必须属于本项目",
            "found": task_id,
        }
    for key in ("title", "impact", "owner", "nextStep"):
        _check_text(issue.get(key), "%s.%s" % (path, key), errors)

    status = issue.get("status")
    if status not in ISSUE_STATUSES:
        errors[path + ".status"] = {
            "code": "intake_enum_invalid",
            "allowed": list(ISSUE_STATUSES),
            "found": status,
        }

    resolution = issue.get("resolutionEvidence")
    if status == "closed":
        if not isinstance(resolution, list) or not resolution:
            errors[path + ".resolutionEvidence"] = {
                "code": "intake_issue_resolution_missing",
                "reason": "closed 必须带非空 resolutionEvidence 材料索引数组",
            }
            return
    if resolution is None:
        return
    if not isinstance(resolution, list):
        errors[path + ".resolutionEvidence"] = {"code": "intake_type_invalid", "expected": "array"}
        return
    document_count = task_document_counts.get(task_id)
    for position, index in enumerate(resolution):
        index_path = "%s.resolutionEvidence[%d]" % (path, position)
        if not _is_non_neg_int(index):
            errors[index_path] = {"code": "intake_material_index_invalid", "found": index}
            continue
        if document_count is None or index >= document_count:
            errors[index_path] = {
                "code": "intake_material_index_invalid",
                "found": index,
                "documents": document_count,
            }


def _validate_project(project, path, errors):
    if not isinstance(project, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "项目必须是对象"}
        return
    _required_keys(project, PROJECT_REQUIRED, path, errors)
    _unknown_keys(project, PROJECT_ALLOWED, path, errors)

    if not _is_id(project.get("projectId")):
        errors[path + ".projectId"] = {
            "code": "intake_identifier_invalid",
            "found": project.get("projectId"),
        }
    for key in ("name", "goal", "owner", "stage", "currentWork", "nextStep"):
        _check_text(project.get(key), "%s.%s" % (path, key), errors)
    _check_abs_path(project.get("workspace"), path + ".workspace", errors)

    milestones = project.get("milestones")
    milestone_ids = set()
    if _check_collection(milestones, path + ".milestones", errors,
                         limit=MAX_MILESTONES_PER_PROJECT):
        for index, milestone in enumerate(milestones):
            _validate_milestone(milestone, "%s.milestones[%d]" % (path, index), errors)
            if isinstance(milestone, dict):
                milestone_ids.add(milestone.get("id"))
        _dedupe([m.get("id") for m in milestones if isinstance(m, dict)],
                "milestoneId", errors, path + ".milestones")
        # 阶段 id 必须在项目内唯一（上一步已查重），此处只保留合法集合
        milestone_ids = {value for value in milestone_ids if _is_id(value)}

    sources = project.get("sources")
    if _check_collection(sources, path + ".sources", errors,
                         limit=MAX_SOURCES_PER_PROJECT, allow_empty=False):
        for index, source in enumerate(sources):
            _validate_source(source, "%s.sources[%d]" % (path, index), errors)

    tasks = project.get("tasks")
    task_document_counts = {}
    if _check_collection(tasks, path + ".tasks", errors, limit=MAX_TASKS_PER_PROJECT):
        for index, task in enumerate(tasks):
            _validate_task(task, "%s.tasks[%d]" % (path, index), errors, milestone_ids)
            if isinstance(task, dict):
                documents = task.get("documents")
                task_document_counts[task.get("taskId")] = (
                    len(documents) if isinstance(documents, list) else 0
                )
        _dedupe([t.get("taskId") for t in tasks if isinstance(t, dict)],
                "taskId", errors, path + ".tasks")

    issues = project.get("issues")
    if _check_collection(issues, path + ".issues", errors, limit=MAX_ISSUES_PER_PROJECT):
        for index, issue in enumerate(issues):
            _validate_issue(issue, "%s.issues[%d]" % (path, index), errors, task_document_counts)
        _dedupe([i.get("issueId") for i in issues if isinstance(i, dict)],
                "issueId", errors, path + ".issues")


def validate_intake_document(obj):
    """``project-intake.v1`` 整体结构校验。

    返回 ``{错误路径: 原因}``；空字典表示通过。fail closed，不自动修复，
    也不依赖任何文件是否存在（存在性/越界/可信读取属于 service 预检）。
    """
    errors = {}
    if not isinstance(obj, dict):
        return {"intake": {"code": "intake_structure_invalid", "reason": "根节点必须是对象"}}

    _check_schema_version(obj, INTAKE_SCHEMA_VERSIONS, UPDATE_SCHEMA_VERSIONS, errors)
    _required_keys(obj, INTAKE_TOP_REQUIRED, "intake", errors)
    _unknown_keys(obj, INTAKE_TOP_ALLOWED, "intake", errors)

    if not _is_id(obj.get("intakeId")):
        errors["intakeId"] = {"code": "intake_identifier_invalid", "found": obj.get("intakeId")}
    if not _is_pos_int(obj.get("intakeRevision")):
        errors["intakeRevision"] = {
            "code": "intake_type_invalid",
            "reason": "intakeRevision 必须是非 bool 的正整数",
            "found": obj.get("intakeRevision"),
        }
    _check_text(obj.get("preparedAt"), "preparedAt", errors)

    _check_identity_block(obj.get("preparedBy"), "preparedBy", PREPARED_BY_REQUIRED, errors)

    projects = obj.get("projects")
    if _check_collection(projects, "projects", errors, limit=MAX_PROJECTS, allow_empty=False):
        for index, project in enumerate(projects):
            _validate_project(project, "projects[%d]" % index, errors)
        _dedupe([p.get("projectId") for p in projects if isinstance(p, dict)],
                "projectId", errors, "projects")
        # taskId 在全目录唯一（接口 §12.2）：跨项目同号同样拒绝
        _dedupe(
            [t.get("taskId") for p in projects if isinstance(p, dict)
             for t in (p.get("tasks") or []) if isinstance(t, dict)],
            "taskId", errors, "projects.tasks",
        )

    return errors


# ---------------------------------------------------------------------------
# project-update.v1 结构校验
# ---------------------------------------------------------------------------

def _validate_update_source_report(report, path, errors):
    if not isinstance(report, dict):
        errors[path] = {"code": "intake_structure_invalid", "reason": "sourceReport 必须是对象"}
        return
    _required_keys(report, SOURCE_REPORT_REQUIRED, path, errors)
    _unknown_keys(report, SOURCE_REPORT_ALLOWED, path, errors)
    if not _is_id(report.get("reportId")):
        errors[path + ".reportId"] = {"code": "intake_identifier_invalid", "found": report.get("reportId")}
    if not _is_pos_int(report.get("reportRevision")):
        errors[path + ".reportRevision"] = {
            "code": "intake_type_invalid",
            "reason": "reportRevision 必须是非 bool 的正整数",
        }
    _check_abs_path(report.get("path"), path + ".path", errors)


def validate_update_document(obj):
    """``project-update.v1`` 整体结构校验。

    明确拒绝运行 / 授权字段（approved / accepted / attemptId / hostRunId /
    permissions 及同族别名）：资料声明不等于运行事实，也不能授予审核权。
    """
    errors = {}
    if not isinstance(obj, dict):
        return {"update": {"code": "intake_structure_invalid", "reason": "根节点必须是对象"}}

    _check_schema_version(obj, UPDATE_SCHEMA_VERSIONS, INTAKE_SCHEMA_VERSIONS, errors)
    _required_keys(obj, UPDATE_TOP_REQUIRED, "update", errors)
    _unknown_keys(obj, UPDATE_TOP_ALLOWED, "update", errors)

    forbidden = sorted(set(obj.keys()) & (set(UPDATE_FORBIDDEN_FIELDS)
                                          | set(UPDATE_FORBIDDEN_ALIASES)))
    if forbidden:
        errors["update.forbiddenFields"] = {
            "code": "update_run_field_forbidden",
            "fields": forbidden,
        }

    if not _is_id(obj.get("updateId")):
        errors["updateId"] = {"code": "intake_identifier_invalid", "found": obj.get("updateId")}
    if not _is_pos_int(obj.get("updateRevision")):
        errors["updateRevision"] = {
            "code": "intake_type_invalid",
            "reason": "updateRevision 必须是非 bool 的正整数",
            "found": obj.get("updateRevision"),
        }
    for key in ("projectId", "taskId"):
        if not _is_id(obj.get(key)):
            errors[key] = {"code": "intake_identifier_invalid", "found": obj.get(key)}

    if not _is_pos_int(obj.get("expectedProjectRevision")):
        errors["expectedProjectRevision"] = {
            "code": "intake_type_invalid",
            "reason": "expectedProjectRevision 必须是非 bool 的正整数",
            "found": obj.get("expectedProjectRevision"),
        }

    if obj.get("kind") not in UPDATE_KINDS:
        errors["kind"] = {
            "code": "intake_enum_invalid",
            "allowed": list(UPDATE_KINDS),
            "found": obj.get("kind"),
        }

    _check_text(obj.get("occurredAt"), "occurredAt", errors)
    _check_identity_block(obj.get("reportedBy"), "reportedBy", PREPARED_BY_REQUIRED, errors)
    _validate_card_ref(obj.get("sourceCard"), "sourceCard", errors)
    _validate_update_source_report(obj.get("sourceReport"), "sourceReport", errors)

    errors.update(validate_report_board_summary(obj.get("boardSummary")))

    if not _present(obj.get("businessSummary")):
        errors["businessSummary"] = {"code": "business_summary_invalid"}
    else:
        errors.update(validate_business_summary(obj["businessSummary"]))

    if not _present(obj.get("evidence")):
        errors["evidence"] = {"code": "evidence_invalid"}
    elif not isinstance(obj.get("evidence"), list):
        errors["evidence"] = {"code": "evidence_invalid"}
    elif len(obj["evidence"]) > MAX_EVIDENCE_ITEMS:
        errors["evidence"] = {
            "code": "intake_limit_exceeded",
            "count": len(obj["evidence"]),
            "limit": MAX_EVIDENCE_ITEMS,
        }
    else:
        errors.update(validate_evidence(obj["evidence"]))

    supersedes = obj.get("supersedesUpdateId")
    if supersedes is not None and not _is_plain_str(supersedes):
        errors["supersedesUpdateId"] = {"code": "intake_type_invalid"}

    return errors


# ---------------------------------------------------------------------------
# 占位符与待补项：结构层放行，预检必须标为「待补充」
# ---------------------------------------------------------------------------

def _walk_paths(obj, path=""):
    """遍历所有字符串值，产出 (点号路径, 值)。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _walk_paths(value, "%s.%s" % (path, key) if path else str(key))
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            yield from _walk_paths(value, "%s[%d]" % (path, index))
    elif isinstance(obj, str):
        yield path, obj


def find_placeholders(obj):
    """列出仍使用模板占位符的字段；这些字段**不能**算作可应用。

    返回 ``[{"path": ..., "value": ...}, ...]``，按路径排序，便于前后比对。
    """
    found = [
        {"path": path, "value": value}
        for path, value in _walk_paths(obj)
        if path and _is_placeholder(value)
    ]
    found.sort(key=lambda item: item["path"])
    return found


def summarize_intake(obj):
    """只读统计：项目/任务/问题/来源数量与去重后编号，供预检清单使用。

    纯计算，不读文件、不写任何状态；对象非法时返回空统计而不是抛错。
    """
    if not isinstance(obj, dict):
        return {"projects": 0, "tasks": 0, "issues": 0, "sources": 0, "projectIds": [],
                "taskIds": [], "issueIds": []}
    projects = obj.get("projects") if isinstance(obj.get("projects"), list) else []
    task_ids, issue_ids = [], []
    tasks = sources = issues = 0
    for project in projects:
        if not isinstance(project, dict):
            continue
        project_tasks = project.get("tasks") if isinstance(project.get("tasks"), list) else []
        project_issues = project.get("issues") if isinstance(project.get("issues"), list) else []
        project_sources = project.get("sources") if isinstance(project.get("sources"), list) else []
        tasks += len(project_tasks)
        issues += len(project_issues)
        sources += len(project_sources)
        for task in project_tasks:
            if isinstance(task, dict) and isinstance(task.get("taskId"), str):
                task_ids.append(task["taskId"])
        for issue in project_issues:
            if isinstance(issue, dict) and isinstance(issue.get("issueId"), str):
                issue_ids.append(issue["issueId"])
    return {
        "projects": len(projects),
        "tasks": tasks,
        "issues": issues,
        "sources": sources,
        "projectIds": sorted({p.get("projectId") for p in projects
                              if isinstance(p, dict) and isinstance(p.get("projectId"), str)}),
        "taskIds": sorted(set(task_ids)),
        "issueIds": sorted(set(issue_ids)),
    }


def collect_material_paths(obj):
    """列出资料声明的来源文件路径（sources / documents.sourcePath / cardRefs.path）。

    返回 ``[{"path": ..., "role": ..., "pointer": ...}, ...]``；只做收集，
    不读文件、不判断可读性。用于预检逐项安全读取与登记实际 hash。
    """
    items = []
    if not isinstance(obj, dict):
        return items
    projects = obj.get("projects") if isinstance(obj.get("projects"), list) else []
    for project_index, project in enumerate(projects):
        if not isinstance(project, dict):
            continue
        for source_index, source in enumerate(project.get("sources") or []):
            if isinstance(source, dict) and isinstance(source.get("path"), str):
                items.append({
                    "path": source["path"],
                    "role": "source",
                    "pointer": "projects[%d].sources[%d]" % (project_index, source_index),
                })
        for task_index, task in enumerate(project.get("tasks") or []):
            if not isinstance(task, dict):
                continue
            for doc_index, document in enumerate(task.get("documents") or []):
                if isinstance(document, dict) and isinstance(document.get("sourcePath"), str):
                    items.append({
                        "path": document["sourcePath"],
                        "role": "material",
                        "pointer": "projects[%d].tasks[%d].documents[%d]"
                                   % (project_index, task_index, doc_index),
                    })
            for ref_index, card_ref in enumerate(task.get("cardRefs") or []):
                if isinstance(card_ref, dict) and isinstance(card_ref.get("path"), str):
                    items.append({
                        "path": card_ref["path"],
                        "role": "card",
                        "pointer": "projects[%d].tasks[%d].cardRefs[%d]"
                                   % (project_index, task_index, ref_index),
                    })
    items.sort(key=lambda item: (item["pointer"], item["path"]))
    return items


def collect_update_paths(obj):
    """列出更新文件声明的来源文件（sourceCard.path / sourceReport.path）。"""
    items = []
    if not isinstance(obj, dict):
        return items
    for role, key in (("card", "sourceCard"), ("report", "sourceReport")):
        block = obj.get(key)
        if isinstance(block, dict) and isinstance(block.get("path"), str):
            items.append({"path": block["path"], "role": role, "pointer": key + ".path"})
    items.sort(key=lambda item: item["pointer"])
    return items


# ---------------------------------------------------------------------------
# 统一入口：按 schemaVersion 分派
# ---------------------------------------------------------------------------

VALIDATORS = {
    "intake": validate_intake_document,
    "update": validate_update_document,
}


def validate_document(obj, doc_type):
    """按显式 doc_type 校验；类型不在词表内直接拒绝（不做模糊猜测）。"""
    if doc_type not in VALIDATORS:
        raise ValueError("unknown intake doc type: %r" % (doc_type,))
    return VALIDATORS[doc_type](obj)


__all__ = [
    "PROJECT_INTAKE_V1",
    "PROJECT_UPDATE_V1",
    "INTAKE_SCHEMA_VERSIONS",
    "UPDATE_SCHEMA_VERSIONS",
    "ID_PATTERN",
    "TASK_CATEGORIES",
    "TASK_STATUSES",
    "ISSUE_STATUSES",
    "UPDATE_KINDS",
    "CARD_REVISION_UNVERIFIED",
    "INTAKE_JSON_MAX_BYTES",
    "MATERIAL_MAX_BYTES",
    "CATALOG_MAX_BYTES",
    "MAX_PROJECTS",
    "MAX_MILESTONES_PER_PROJECT",
    "MAX_SOURCES_PER_PROJECT",
    "MAX_TASKS_PER_PROJECT",
    "MAX_ISSUES_PER_PROJECT",
    "MAX_CARD_REFS_PER_TASK",
    "MAX_DOCUMENTS_PER_TASK",
    "MAX_HISTORY_PER_TASK",
    "MAX_EVIDENCE_ITEMS",
    "UPDATE_FORBIDDEN_FIELDS",
    "UPDATE_FORBIDDEN_ALIASES",
    "INTAKE_TOP_REQUIRED",
    "PROJECT_REQUIRED",
    "TASK_REQUIRED",
    "ISSUE_REQUIRED",
    "UPDATE_TOP_REQUIRED",
    "INTAKE_ERROR_CODES",
    "ProjectIntakeContractError",
    "parse_intake_json",
    "detect_schema_version",
    "validate_intake_document",
    "validate_update_document",
    "validate_document",
    "find_placeholders",
    "summarize_intake",
    "collect_material_paths",
    "collect_update_paths",
]
