"""D4 契约字段定义与结构校验。

对应接口文档《D4 接口与数据约定》第 4/5/6 节：
- 统一消息外壳 event.v1
- 创建任务 task.v1
- 交付记录 delivery.v1
- 审核决定 review-decision.v1

本模块只做结构校验和错误码定义，不含业务规则（去重、序号、权限、状态机在 service/）。
"""
from __future__ import annotations

SCHEMA_VERSION = "1.1"

TASK_V1 = "task.v1"
EVENT_V1 = "event.v1"
DELIVERY_V1 = "delivery.v1"
REVIEW_DECISION_V1 = "review-decision.v1"

# 第一条真实链路支持的事件（接口文档 §5.3）
EVENT_TYPES = (
    "task.accepted",
    "task.started",
    "task.progress",
    "task.blocked",
    "task.delivered",
    "source.heartbeat",
)

# cox 审核决定（接口文档 §5.5）
REVIEW_DECISIONS = ("approved", "changes_requested", "blocked", "request_info")

# 错误码 -> 大白话（接口文档 §6 的“常见错误”表）。
# 注：`invalid_event` / `invalid_review`（结构非法）、`path_escape`（路径越界）、
# `submit_in_progress` / `submit_unknown`（提交去重与未知结果）是本切片新增的扩展码，
# 接口文档 §6 明确写的是“常见错误”而非穷举清单；它们都 fail closed，不写入任何乐观状态。
ERROR_CODES = {
    "invalid_task": "任务说明不完整，不创建任务，返回缺失字段",
    "permission_denied": "没有这个项目或目录的权限，拒绝且不自动重试",
    "unknown_source": "来源还没登记，进入人工核对，不创建卡片",
    "duplicate_event": "这条消息已经收过，返回原接收结果，不重复更新",
    "event_conflict": "同一个消息编号内容变了，拒绝并保留冲突记录",
    "stale_sequence": "这条消息比当前记录旧，留历史，不回退当前状态",
    "invalid_transition": "当前状态不能做这个动作，返回当前状态和允许动作",
    "review_version_mismatch": "审核的是旧交付，重新打开当前任务详情",
    "host_unconfirmed": "只知道已提交，环境还没确认",
    # 扩展码（fail closed）
    "invalid_event": "事件结构不完整或不合法，拒绝并记录",
    "invalid_review": "审核请求结构不完整或不合法，拒绝",
    "path_escape": "路径越出受控 workspace/writeScope，拒绝且不改当前状态",
    "submit_in_progress": "该执行轮次正在提交中，不重复调用宿主",
    "submit_unknown": "上次提交结果未知（可能已发出），不自动重试，人工核对",
    # D4-NEXT-01C 新增：报告导入绑定不一致（只补充新码，不改既有语义）
    "report_binding_mismatch": "报告绑定的项目/任务/执行轮次与当前事实不一致，拒绝写入摘要",
    # D4-NEXT-01C-R1 新增：报告引用的来源任务卡与库内冻结登记不一致（fail closed）
    "source_card_mismatch": "报告引用的来源任务卡不存在或不匹配，拒绝写入摘要",
}

# 任务创建必须显式声明的权限开关；缺一不可，避免“默认放开”。
REQUIRED_PERMISSION_KEYS = (
    "writeScope",
    "allowNetwork",
    "allowBusinessWrite",
    "allowGit",
    "allowPublish",
)
# 必须为真 bool 的开关（拒绝 0/1/字符串等“看似为假”的写法）。
BOOL_PERMISSION_KEYS = ("allowNetwork", "allowBusinessWrite", "allowGit", "allowPublish")
# 任一为真即拒绝：本验证切片不允许联网/业务写入/Git/发布。
FORBIDDEN_PERMISSION_KEYS = BOOL_PERMISSION_KEYS

# limits 严格类型与范围（R1：maxAttempts 不得超过一次返工上限 = 2）。
LIMIT_RANGES = {
    "maxAttempts": (1, 2),
    "maxReviewRounds": (1, 100),
    "timeoutMinutes": (1, 24 * 60),
}

# D4-NEXT-01C-R2：JSON 建卡 sourceCard 引用的三字段必填 + 严格类型。
# path/cardId/cardRevision 缺一、省略、null、空或类型异常一律拒绝；
# cardRevision 必须是 >=1 的整数，显式排除 bool（True/False 是 int 子类）与字符串。
SOURCE_CARD_REFERENCE_FIELDS = ("path", "cardId", "cardRevision")


class ContractError(ValueError):
    """带错误码的契约/业务错误。

    调用方（服务、适配器、投影）遇到本异常必须 fail closed：不写乐观状态、不自动重试。
    """

    def __init__(self, code, message=None, details=None):
        if code not in ERROR_CODES:
            raise ValueError("unknown error code: %r" % (code,))
        self.code = code
        self.details = details or {}
        super().__init__(message or ERROR_CODES[code])

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def _present(value):
    """值是否算“已提供”。空字符串、空列表、空字典视为未提供。"""
    return value not in (None, "", [], {})


def _missing(payload, fields):
    return [f for f in fields if not _present(payload.get(f))]


def validate_permissions(permissions):
    """校验 task.v1 的 permissions 结构（键齐全 + 真 bool + writeScope 数组）。"""
    errors = {}
    if not isinstance(permissions, dict):
        return {"permissions": "permissions 必须是对象"}
    missing = [k for k in REQUIRED_PERMISSION_KEYS if k not in permissions]
    if missing:
        errors["permissions_missing"] = missing
    for key in BOOL_PERMISSION_KEYS:
        if key in permissions and not isinstance(permissions[key], bool):
            errors["permissions.%s" % key] = "必须是布尔值（true/false）"
    scope = permissions.get("writeScope")
    if not isinstance(scope, list) or not scope:
        errors["permissions.writeScope"] = "必须是非空路径数组"
    else:
        for index, entry in enumerate(scope):
            if not isinstance(entry, str) or not entry.strip():
                errors["permissions.writeScope[%d]" % index] = "必须是非空字符串"
    return errors


def validate_limits(limits):
    """校验 limits 严格整型与范围；None 表示未提供（由服务补默认值）。"""
    if limits is None:
        return {}
    if not isinstance(limits, dict):
        return {"limits": "limits 必须是对象"}
    errors = {}
    for key, value in limits.items():
        if key not in LIMIT_RANGES:
            errors["limits.%s" % key] = "未知限制项"
            continue
        low, high = LIMIT_RANGES[key]
        if isinstance(value, bool) or not isinstance(value, int):
            errors["limits.%s" % key] = "必须是整数"
            continue
        if value < low or value > high:
            errors["limits.%s" % key] = "必须在 %d..%d 之间" % (low, high)
    return errors


def is_strict_card_revision(value):
    """受控版本号：>=1 的整数；显式排除 bool（True/False 是 int 子类）与字符串。"""
    return not isinstance(value, bool) and isinstance(value, int) and value >= 1


def validate_source_card_reference(ref):
    """校验 sourceCard 引用对象的必填与严格类型；返回 {字段: 原因}。

    只做结构与类型校验：不做受控读取、不核对库内登记、不信任自报摘要
    （受控读取与库核对属于受控入口与服务层）。空字典表示通过。
    """
    if not isinstance(ref, dict):
        return {"sourceCard": "必须是对象"}
    errors = {}
    for field in SOURCE_CARD_REFERENCE_FIELDS:
        if field not in ref or ref.get(field) is None:
            errors["sourceCard.%s" % field] = "必填，不能省略或为 null"
    path = ref.get("path")
    if "sourceCard.path" not in errors and (not isinstance(path, str) or not path.strip()):
        errors["sourceCard.path"] = "必须是非空字符串路径"
    card_id = ref.get("cardId")
    if "sourceCard.cardId" not in errors and (not isinstance(card_id, str) or not card_id.strip()):
        errors["sourceCard.cardId"] = "必须是非空字符串"
    revision = ref.get("cardRevision")
    if "sourceCard.cardRevision" not in errors and not is_strict_card_revision(revision):
        errors["sourceCard.cardRevision"] = "必须是 >=1 的整数（拒绝布尔/字符串/0/负数）"
    return errors


def validate_create_task(payload):
    """校验 POST /v1/tasks 的请求结构，返回 {字段: 原因}；空字典表示通过。"""
    if not isinstance(payload, dict):
        return {"payload": "请求体必须是对象"}
    errors = {}
    missing = _missing(
        payload, ("projectId", "title", "agentId", "sourceId", "workspace", "goal")
    )
    if missing:
        errors["missing"] = missing

    workspace = payload.get("workspace")
    if _present(workspace) and not str(workspace).startswith("/"):
        errors["workspace"] = "workspace 必须是绝对路径"

    for key, value in validate_permissions(payload.get("permissions")).items():
        errors[key] = value

    for key, value in validate_limits(payload.get("limits")).items():
        errors[key] = value

    acceptance = payload.get("acceptance")
    if acceptance is not None and not isinstance(acceptance, list):
        errors["acceptance"] = "acceptance 必须是数组"

    return errors


def validate_delivery(payload):
    """校验 task.delivered 的 payload（delivery.v1）。"""
    if not isinstance(payload, dict):
        return {"payload": "交付 payload 必须是对象"}
    errors = {}
    missing = [k for k in ("summary", "reportPath") if not _present(payload.get(k))]
    if missing:
        errors["missing"] = missing
    for key in ("changedFiles", "verification", "risks", "unfinished"):
        if key not in payload:
            errors.setdefault("missing", []).append(key)
        elif not isinstance(payload[key], list):
            errors[key] = "必须是数组"
    return errors


def validate_event(payload):
    """校验 POST /v1/events 的统一外壳（event.v1）。"""
    if not isinstance(payload, dict):
        return {"payload": "事件体必须是对象"}
    errors = {}
    missing = _missing(
        payload,
        (
            "eventId",
            "projectId",
            "taskId",
            "attemptId",
            "sourceId",
            "agentId",
            "sequence",
            "type",
            "occurredAt",
        ),
    )
    if missing:
        errors["missing"] = missing

    event_type = payload.get("type")
    if _present(event_type) and event_type not in EVENT_TYPES:
        errors["type"] = "未知事件类型: %r" % (event_type,)

    sequence = payload.get("sequence")
    if sequence is not None and (isinstance(sequence, bool) or not isinstance(sequence, int)):
        errors["sequence"] = "sequence 必须是整数"

    if not isinstance(payload.get("payload", {}), dict):
        errors["payload"] = "payload 必须是对象"

    if event_type == "task.delivered":
        nested = validate_delivery(payload.get("payload") or {})
        for key, value in nested.items():
            errors["delivery.%s" % key] = value

    return errors


def validate_review_decision(payload):
    """校验 POST /v1/tasks/{taskId}/reviews（review-decision.v1）。

    注：审核者身份不在这里取；它必须来自受控入口注入的调用者上下文
    （service/context.py），不得信任 payload 自报的 reviewerId / role。
    """
    if not isinstance(payload, dict):
        return {"payload": "审核体必须是对象"}
    errors = {}
    decision = payload.get("decision")
    if decision not in REVIEW_DECISIONS:
        errors["decision"] = "decision 必须是 %s 之一" % (", ".join(REVIEW_DECISIONS),)
    if not _present(payload.get("deliveryId")):
        errors["deliveryId"] = "必须指定审核的 deliveryId"
    note = payload.get("note")
    if decision == "changes_requested" and not (str(note or "").strip()):
        errors["note"] = "changes_requested 必须给出具体返工说明"
    if decision == "blocked" and not (str(note or "").strip()):
        errors["note"] = "blocked 必须说明等待谁、等待什么、什么条件下恢复"
    return errors
