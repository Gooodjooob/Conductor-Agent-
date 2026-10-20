"""D4-DSH-MEMBER-MODEL-PREFLIGHT-R1 / R2：离线的派发前成员 / 模型只读预检。

只新增本模块与 ``tests/test_d4_dsh_member_preflight.py``。不实现 CLI / HTTP / Adapter、
成员写服务、迁移器、宿主配置；不改 ``service.registry_read``、数据库迁移、正式库；
不启动宿主、不访问网络 / 凭据、不解除 DSH 真实授权门（``_dsh_sdk_authorized()``）。

公开 API（仅一个）::

    preflight_member(conn, *, registry_id, caller, request, evaluated_at,
                     required_checks=None)

职责：按调用方显式提供的受控 ``CallerContext``（只接受 owner / reviewer）与请求
（``agentId`` / ``agentAlias`` / ``projectId`` / ``modelProfileId``），在同一只读快照
内判断目标候选成员当前**声明**是否具备接收任务的条件，返回可审计的
``allow`` / ``deny`` / ``recheck`` 结果。它只是**离线声明一致性核验**，不是可执行性
证明，也不是 OS 隔离或真实宿主认证；结果里所有维度都带 ``evidenceLevel``，
声明层一律 ``UNVERIFIED``。

设计约束：

- 复用已冻结的只读投影：内部调用 ``service.registry_read.list_registry_agents``
  取得同快照、已完整校验的成员投影；本模块**不自建第二套 SQL 读取口径**，不复制
  迁移器 / validator，不修改任何旧模块。
- 只读：不写 SQLite、不建表、不迁移、不改连接配置与 ``row_factory``；只读事务由
  被复用的列表服务持有（deferred ``BEGIN`` / ``COMMIT``）。
- 身份只来自受控入口注入的 ``CallerContext``：``None`` / 类型非法 / worker / source
  一律 ``permission_denied``；请求体里出现任何自称身份（``caller`` / ``role`` /
  ``permissionCeiling`` 等）或未冻结字段一律拒绝，绝不采信 payload 自报。
- 确定性：不调用时钟、不读环境、不联网。有效期判定使用调用方显式传入的
  ``evaluated_at``（必须带时区），同一输入必然得到同一输出。
- 不自动刷新、不自动启用、不自动重试：缺失 / 过期 / 未核验的 check 只会得到
  ``recheck``，失败的 check 得到 ``deny``。

R2 修正的判定语义（本卡冻结，替换 R1 的“同类型记录集合优先级”）：

1. 每种 check 类型**只看绑定当前引用的最新一条记录**，不再让同类型的其它记录参与判定：
   旧 ``failed`` 不得压过更新的 ``passed``；旧 ``passed`` 不得压过更新的 ``failed``；
   最新通过记录过期 / 有效期不明时一律 ``recheck``，**不回退**采用旧的仍有效记录。
2. “最新”按带时区语义比较 ``observedAt``（同一时刻的不同时区写法等价）；时刻完全相同时按
   固定且文档化的 ``checkId`` 升序（Python 字符串比较）取第一条，保证同一输入输出确定。
3. 当前绑定记录的 ``observedAt`` 缺失 / 不可解析 / 无时区时 fail closed 为
   ``recheck``（state ``observed_at_invalid``），绝不以旧记录放行；即便该无效记录
   ``status=failed`` 也不降级为 ``deny``（``recheck`` 同样不放行）。
4. 只有当该类型**不存在**任何绑定当前引用的记录时，旧的绑定记录才归为 ``stale``。
5. 派发前完整预检固定要求 ``config`` / ``identity`` / ``model`` / ``runtime`` 四类 check；
   ``required_checks`` 允许显式传入，但只接受“恰好等于这四类”的集合，任何缩减、增加、
   重复或非法集合一律按 ``invalid_task`` 拒绝，调用方无法缩小必查集合后拿到 ``allow``。
"""
from __future__ import annotations

from datetime import datetime

from contracts.agent_registry import _ID_RE, normalize_agent_alias
from contracts.schemas import ContractError

import storage.registry_migrations as _migrations

# 只读复用受控入口上下文契约与 02B1A 已审核的入参 / 连接校验 helper，不改旧模块。
from service.context import REVIEW_ROLES, require_context
from service.registry_read import (
    REASON_CONNECTION_IN_TRANSACTION,
    REASON_INVALID_ARGUMENT,
    _check_row_factory,
    _require_connection,
    list_registry_agents,
)

# ---------------------------------------------------------------------------
# 常量（本卡冻结）
# ---------------------------------------------------------------------------
PREFLIGHT_SCHEMA_VERSION = "dsh-member-preflight.v1"

DECISION_ALLOW = "allow"
DECISION_DENY = "deny"
DECISION_RECHECK = "recheck"
DECISIONS = (DECISION_ALLOW, DECISION_DENY, DECISION_RECHECK)

# 原因等级：deny 立即否决；recheck 表示离线无法确认（不含否决），需人工 / 后续核验。
LEVEL_DENY = "deny"
LEVEL_RECHECK = "recheck"

# 请求只接受这四个字段；出现其它字段即拒绝（未冻结字段不允许扩展语义）。
REQUEST_FIELDS = ("agentId", "agentAlias", "projectId", "modelProfileId")

# 请求体里任何自称身份 / 权限 / 预置结论的字段：直接按“自报身份”拒绝，不静默忽略。
_SELF_REPORTED_IDENTITY_FIELDS = frozenset({
    "caller", "context", "actor", "actorId", "actor_id", "role", "roles",
    "owner", "ownerId", "reviewer", "reviewerId", "workerId", "sourceId",
    "capabilities", "permissions", "permissionCeiling", "allowedRoots", "writeScope",
    "enabled", "registrationComplete", "normalizedAlias", "agentNormalizedAlias",
    "decision", "reasons", "evidenceLevel", "evidence", "checks", "binding",
    "environmentId", "environmentRevision", "bindingRevision", "modelProfile",
    "modelProfileRevision", "configDigest", "adapterType", "hostAgentId",
})

# check_type 词表与 §13.8 DDL CHECK 一致；默认需要 config / identity / model / runtime。
CHECK_TYPES = ("config", "identity", "login", "model", "runtime")
# 完整派发预检的必查集合：固定四类，调用方不得缩减（R2 冻结）。
DEFAULT_REQUIRED_CHECKS = ("config", "identity", "model", "runtime")

# 证据等级词表与 §13.8 DDL CHECK 一致；本模块从不生成 OFFICIAL，只如实透传已登记值。
EVIDENCE_LEVELS = ("OFFICIAL", "LOCAL", "SYNTHETIC", "UNVERIFIED")
EVIDENCE_UNVERIFIED = "UNVERIFIED"

# 受控拒绝原因（不新增全局错误码，只在本模块 details.reason 中使用）。
REASON_SELF_REPORTED_IDENTITY = "self_reported_identity_not_allowed"
REASON_UNKNOWN_REQUEST_FIELD = "unknown_request_field"
# 传入的 required_checks 不是完整四类必查集合（缩减 / 增加）时使用。
REASON_REQUIRED_CHECKS_NOT_FULL = "required_checks_not_full"

# 成员级 check 判定状态。
CHECK_SATISFIED = "satisfied"
CHECK_FAILED = "failed"
CHECK_EXPIRED = "expired"
CHECK_EXPIRY_UNKNOWN = "expiry_unknown"
CHECK_EVIDENCE_UNVERIFIED = "evidence_unverified"
CHECK_STATUS_UNVERIFIED = "unverified"
CHECK_OBSERVED_AT_INVALID = "observed_at_invalid"
CHECK_STALE = "stale"
CHECK_MISSING = "missing"

# 状态 → (是否满足, reason code, 等级)
_CHECK_STATE_RULES = {
    CHECK_FAILED: (False, "check_failed", LEVEL_DENY),
    CHECK_EXPIRED: (False, "check_expired", LEVEL_RECHECK),
    CHECK_EXPIRY_UNKNOWN: (False, "check_expiry_unknown", LEVEL_RECHECK),
    CHECK_EVIDENCE_UNVERIFIED: (False, "check_evidence_unverified", LEVEL_RECHECK),
    CHECK_STATUS_UNVERIFIED: (False, "check_unverified", LEVEL_RECHECK),
    CHECK_OBSERVED_AT_INVALID: (False, "check_observed_at_invalid", LEVEL_RECHECK),
    CHECK_STALE: (False, "check_stale", LEVEL_RECHECK),
    CHECK_MISSING: (False, "check_missing", LEVEL_RECHECK),
}


# ---------------------------------------------------------------------------
# 内部工具：受控拒绝
# ---------------------------------------------------------------------------
def _invalid(field, reason):
    """统一失败出口：既有 ``ContractError(invalid_task)`` + 稳定 ``field`` / ``reason``。"""
    raise ContractError(
        "invalid_task",
        message="派发前预检拒绝：%s（field=%s）" % (reason, field),
        details={"field": field, "reason": reason},
    )


def _denied(message, **details):
    """受控入口身份不足：``permission_denied``，默认拒绝。"""
    raise ContractError("permission_denied", message=message, details=details)


def _reason(code, level, field, detail):
    return {"code": code, "level": level, "field": field, "detail": detail}


# ---------------------------------------------------------------------------
# 入参校验（错误参数先拒绝，不执行任何查询）
# ---------------------------------------------------------------------------
def _validate_registry_id(value):
    """registryId：§13.8 受控 ID 完整匹配，不省略 / null / 空 / 尾 LF。"""
    if not isinstance(value, str) or _migrations._REGISTRY_ID_PATTERN.fullmatch(value) is None:
        _invalid("registryId", REASON_INVALID_ARGUMENT)


def _validate_controlled_id(value, field):
    """受控 ID：§13.7 完整匹配，一次拒绝非字符串 / 空 / 尾 LF / 路径 / URL / 控制字符。"""
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _invalid(field, REASON_INVALID_ARGUMENT)


def _validate_request(request):
    """请求体严格校验：未知字段 / 自报身份字段一律拒绝，返回规范化后的四元组。

    - 必须是对象；只允许 ``agentId`` / ``agentAlias`` / ``projectId`` / ``modelProfileId``。
    - 出现字段但值为 ``null`` 视为非法（不把“显式 null”当作“未提供”）。
    - ``projectId`` 必填：任务是项目内的，缺项目关联无从判断。
    - ``agentId`` / ``agentAlias`` 至少提供一个；两者都给时必须互为同一成员（在解析阶段核）。
    """
    if not isinstance(request, dict):
        _invalid("request", REASON_INVALID_ARGUMENT)
    for key in request:
        if key in _SELF_REPORTED_IDENTITY_FIELDS:
            # 身份 / 权限 / 结论只能来自受控入口，payload 自报一律拒绝。
            _invalid("request.%s" % key, REASON_SELF_REPORTED_IDENTITY)
        if key not in REQUEST_FIELDS:
            _invalid("request.%s" % key, REASON_UNKNOWN_REQUEST_FIELD)

    agent_id = request.get("agentId")
    agent_alias = request.get("agentAlias")
    project_id = request.get("projectId")
    model_profile_id = request.get("modelProfileId")

    for key, value in (
        ("agentId", agent_id),
        ("agentAlias", agent_alias),
        ("projectId", project_id),
        ("modelProfileId", model_profile_id),
    ):
        if key in request and value is None:
            _invalid("request.%s" % key, REASON_INVALID_ARGUMENT)

    if agent_id is not None:
        _validate_controlled_id(agent_id, "request.agentId")
    if project_id is None:
        _invalid("request.projectId", REASON_INVALID_ARGUMENT)
    _validate_controlled_id(project_id, "request.projectId")
    if model_profile_id is not None:
        _validate_controlled_id(model_profile_id, "request.modelProfileId")

    normalized_alias = None
    if agent_alias is not None:
        try:
            normalized_alias = normalize_agent_alias(agent_alias)
        except ContractError:
            # 00A1 冻结的别名规范化失败（非字符串 / 控制字符 / 空白 / 超长）→ 入参非法。
            _invalid("request.agentAlias", REASON_INVALID_ARGUMENT)

    if agent_id is None and normalized_alias is None:
        _invalid("request", REASON_INVALID_ARGUMENT)

    return agent_id, agent_alias, normalized_alias, project_id, model_profile_id


def _validate_evaluated_at(value):
    """``evaluatedAt``：必须能解析成带时区的 ISO-8601；本模块不读系统时钟。

    显式注入时间而不是取 ``datetime.now()``，是为了让“同一输入必然同一输出”可复核；
    无时区偏移的朴素时间会被拒绝，避免本地时区不同导致有效期判定漂移。
    """
    return _parse_timestamp(value, "evaluatedAt", REASON_INVALID_ARGUMENT)


def _parse_timestamp(value, field, reason):
    if not isinstance(value, str) or not value:
        _invalid(field, reason)
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        _invalid(field, reason)
    if parsed.tzinfo is None:
        _invalid(field, reason)
    return parsed


def _validate_required_checks(value):
    """必需 check 类型：``None`` 用默认四类；显式传入时必须**恰好**是完整四类集合。

    派发前完整预检固定要求 ``config`` / ``identity`` / ``model`` / ``runtime``（R2 冻结）：
    调用方不得用 ``required_checks`` 缩减必查集合后拿到 ``allow``。校验顺序固定：

    1. ``None`` → 采用默认四类并返回规范顺序（顺序不影响判定，输出永远规范顺序）。
    2. 非 ``list`` / ``tuple``、空集合 → ``invalid_argument``。
    3. 元素非字符串、不在受控词表、重复 → ``invalid_argument``（重复先于集合比较）。
    4. 集合与默认四类不相等（缩减或增加，如 ``("config",)`` / ``(..., "login")``）→
       ``required_checks_not_full``。
    """
    if value is None:
        return DEFAULT_REQUIRED_CHECKS
    if not isinstance(value, (list, tuple)) or not value:
        _invalid("requiredChecks", REASON_INVALID_ARGUMENT)
    seen = []
    for item in value:
        if not isinstance(item, str) or item not in CHECK_TYPES:
            _invalid("requiredChecks", REASON_INVALID_ARGUMENT)
        if item in seen:
            _invalid("requiredChecks", REASON_INVALID_ARGUMENT)
        seen.append(item)
    if set(seen) != set(DEFAULT_REQUIRED_CHECKS):
        # 缩减（漏掉必需类型）与增加（塞入 login 等额外类型）都拒绝，不静默规范化。
        _invalid("requiredChecks", REASON_REQUIRED_CHECKS_NOT_FULL)
    return DEFAULT_REQUIRED_CHECKS


def _require_reviewer_caller(caller):
    """身份门槛：必须由受控入口注入 owner / reviewer 上下文，其余一律拒绝。

    ``require_context`` 负责 ``None`` / 类型非法（默认拒绝）；本函数再收紧角色：
    worker 与 source 上下文不能发起派发前预检。
    """
    require_context(caller, None)
    if caller.role not in REVIEW_ROLES:
        _denied(
            "派发前预检需要受控 owner / reviewer 上下文",
            actorId=caller.actor_id,
            role=caller.role,
            entry=caller.entry,
            allowedRoles=list(REVIEW_ROLES),
        )
    return caller


# ---------------------------------------------------------------------------
# 成员解析（只按受控 agentId 主键 / normalizedAlias 精确值，不模糊匹配）
# ---------------------------------------------------------------------------
def _resolve_member(items, agent_id, normalized_alias):
    """返回 ``(member, reason)``；解析失败时 member 为 ``None`` 且给出唯一拒绝原因。"""
    member = None
    if agent_id is not None:
        for item in items:
            if item["agentId"] == agent_id:
                member = item
                break
        if member is None:
            return None, _reason(
                "member_not_found", LEVEL_DENY, "request.agentId",
                "指定 agentId 在候选 Registry 中不存在",
            )

    if normalized_alias is not None:
        candidates = [item for item in items if item["normalizedAlias"] == normalized_alias]
        if not candidates:
            # 只按 normalizedAlias 精确值解析；不做前缀 / 大小写猜测 / 显示名匹配。
            return None, _reason(
                "member_not_found", LEVEL_DENY, "request.agentAlias",
                "normalizedAlias 精确解析无匹配成员（不做模糊匹配）",
            )
        if len(candidates) > 1:
            return None, _reason(
                "alias_ambiguous", LEVEL_DENY, "request.agentAlias",
                "normalizedAlias 命中多个成员，拒绝歧义解析",
            )
        if member is not None and candidates[0]["agentId"] != agent_id:
            return None, _reason(
                "alias_agent_mismatch", LEVEL_DENY, "request.agentAlias",
                "agentId 与 agentAlias 指向不同成员",
            )
        member = candidates[0]
    return member, None


# ---------------------------------------------------------------------------
# check 判定（按绑定版本 + 有效期逐项判断，不自动刷新 / 不自动启用）
# ---------------------------------------------------------------------------
def _expiry_state(check, evaluated_at):
    """有效期三态：``valid`` / ``expired`` / ``unknown``（缺时区或不可解析）。"""
    try:
        parsed = _parse_timestamp(check["expiresAt"], "expiresAt", REASON_INVALID_ARGUMENT)
    except ContractError:
        return "unknown"
    return "valid" if parsed > evaluated_at else "expired"


def _is_current_bound(check, check_type, environment, binding, model_profile):
    """check 是否绑定到成员**当前**环境版本 / 绑定版本 / 配置摘要（model 另核模型版本）。"""
    if (
        check["bindingRevision"] != binding["bindingRevision"]
        or check["environmentId"] != environment["environmentId"]
        or check["environmentRevision"] != environment["revision"]
        or check["configDigest"] != binding["configDigest"]
    ):
        return False
    if check_type == "model":
        return (
            check["modelProfileId"] == model_profile["modelProfileId"]
            and check["modelProfileRevision"] == model_profile["profileRevision"]
        )
    return True


def _parse_observed_at(check):
    """``observedAt`` 解析：正确时返回带时区 ``datetime``，否则返回 ``None``。

    与 ``expiresAt`` 不同：这里不抛错。``observedAt`` 非法要在判定阶段 fail closed 成
    ``recheck``（state ``observed_at_invalid``），而不是把整次预检变成入参错误。
    """
    try:
        return _parse_timestamp(check["observedAt"], "observedAt", REASON_INVALID_ARGUMENT)
    except ContractError:
        return None


def _select_latest(records):
    """在记录集合里选“最新记录”：返回 ``(selected, observed_at_invalid)``。

    次序固定且文档化（同一输入必然同一输出）：

    1. 先按**带时区语义**比较 ``observedAt``（同一时刻的不同时区写法等价），取最大者为最新；
    2. 时刻完全相同时按 ``checkId`` 升序（Python 字符串比较）取第一条；
    3. 只要集合里存在无法按时区解析的 ``observedAt``，就不猜“谁更新”，fail closed
       返回 ``(checkId 最小的该记录, True)``。
    """
    timed = []
    untimed = []
    for check in records:
        parsed = _parse_observed_at(check)
        if parsed is None:
            untimed.append(check)
        else:
            timed.append((parsed, check))
    if untimed:
        untimed.sort(key=lambda check: check["checkId"])
        return untimed[0], True
    latest = max(observed_at for observed_at, _check in timed)
    candidates = [check for observed_at, check in timed if observed_at == latest]
    candidates.sort(key=lambda check: check["checkId"])
    return candidates[0], False


def _select_stale(rows):
    """旧绑定记录的审计展示选择：优先最新可解析记录，其次 ``checkId`` 升序第一条。"""
    timed = []
    untimed = []
    for check in rows:
        parsed = _parse_observed_at(check)
        if parsed is None:
            untimed.append(check)
        else:
            timed.append((parsed, check))
    if timed:
        latest = max(observed_at for observed_at, _check in timed)
        candidates = [check for observed_at, check in timed if observed_at == latest]
        candidates.sort(key=lambda check: check["checkId"])
        return candidates[0]
    untimed.sort(key=lambda check: check["checkId"])
    return untimed[0]


def _classify_check_type(rows, bound, evaluated_at):
    """判定一个 check_type：只取绑定当前引用的**最新一条**记录，按该条记录定状态。

    优先级固定（保守 fail closed，且全部只作用于最新记录本身）：

    1. 该类型没有任何绑定当前引用的记录：有旧绑定记录 → ``stale``；否则 ``missing``。
    2. 最新记录 ``observedAt`` 无效（缺失 / 非字符串 / 无时区 / 不可解析）→
       ``observed_at_invalid``（recheck，即使该记录 ``status=failed`` 也不降级为 deny）。
    3. ``status=failed`` → ``failed``（deny）。
    4. ``status=passed``：``expiresAt`` 已过期 → ``expired``；有效期不明 → ``expiry_unknown``；
       ``evidenceLevel=UNVERIFIED`` → ``evidence_unverified``；否则 ``satisfied``。
    5. 其余（含 ``status=unverified``）→ ``unverified``。

    同类型的更旧记录一律不参与判定：更早的 ``failed`` 不会压过更新的 ``passed``，更早的
    仍有效 ``passed`` 也不会回填更新的过期 / 失败记录。同一输入必然同一分类。
    """
    if not bound:
        if rows:
            return CHECK_STALE, _select_stale(rows)
        return CHECK_MISSING, None

    selected, observed_at_invalid = _select_latest(bound)
    if observed_at_invalid:
        return CHECK_OBSERVED_AT_INVALID, selected

    if selected["status"] == "failed":
        return CHECK_FAILED, selected
    if selected["status"] == "passed":
        expiry = _expiry_state(selected, evaluated_at)
        if expiry == "expired":
            return CHECK_EXPIRED, selected
        if expiry == "unknown":
            return CHECK_EXPIRY_UNKNOWN, selected
        if selected["evidenceLevel"] == EVIDENCE_UNVERIFIED:
            return CHECK_EVIDENCE_UNVERIFIED, selected
        return CHECK_SATISFIED, selected
    return CHECK_STATUS_UNVERIFIED, selected


def _evaluate_checks(member, required_checks, evaluated_at):
    """返回 ``(check_states, reasons, satisfied_by_type)``，全部确定性输出。"""
    environment = member["environment"]
    binding = member["binding"]
    model_profile = member["modelProfile"]

    states = []
    reasons = []
    satisfied_by_type = {}
    for check_type in required_checks:
        rows = [c for c in member["checks"] if c["checkType"] == check_type]
        bound = [
            check for check in rows
            if _is_current_bound(check, check_type, environment, binding, model_profile)
        ]

        state, selected = _classify_check_type(rows, bound, evaluated_at)
        satisfied = state == CHECK_SATISFIED
        satisfied_by_type[check_type] = satisfied
        state_entry = {
            "checkType": check_type,
            "state": state,
            "satisfied": satisfied,
            "checkId": None if selected is None else selected["checkId"],
            "status": None if selected is None else selected["status"],
            "evidenceLevel": None if selected is None else selected["evidenceLevel"],
            "observedAt": None if selected is None else selected["observedAt"],
            "expiresAt": None if selected is None else selected["expiresAt"],
            "boundToCurrentReference": selected is not None and _is_current_bound(
                selected, check_type, environment, binding, model_profile
            ),
        }
        states.append(state_entry)

        if not satisfied:
            _satisfied_rule, code, level = _CHECK_STATE_RULES[state]
            detail = {
                CHECK_FAILED: "该类型 check 最新结果为 failed，否决",
                CHECK_EXPIRED: "该类型 check 最新记录已过 expiresAt，需重新核验（不回退旧记录）",
                CHECK_EXPIRY_UNKNOWN: "该类型 check 最新记录的 expiresAt 无法按时区解析，有效期未知",
                CHECK_EVIDENCE_UNVERIFIED: "该类型 check 最新结果为 passed，但 evidenceLevel=UNVERIFIED，不作为有效证据",
                CHECK_STATUS_UNVERIFIED: "该类型 check 最新记录尚未完成核验（status=unverified）",
                CHECK_OBSERVED_AT_INVALID: "该类型 check 最新绑定记录的 observedAt 无效，无法确定时序，保守要求重新核验",
                CHECK_STALE: "仅有绑定旧 binding / 旧 model / 非当前 configDigest 的 check，视为过期",
                CHECK_MISSING: "缺少该类型的 check 记录",
            }[state]
            reasons.append(_reason(code, level, "checks.%s" % check_type, detail))
    return states, reasons, satisfied_by_type


# ---------------------------------------------------------------------------
# 证据等级（声明层一律 UNVERIFIED；不把声明当 OS 隔离或真实宿主身份）
# ---------------------------------------------------------------------------
def _dimension(evidence_level, declaration_only, **extra):
    entry = {"evidenceLevel": evidence_level, "declarationOnly": declaration_only}
    entry.update(extra)
    return entry


def _check_evidence(check_states, check_type):
    for entry in check_states:
        if entry["checkType"] == check_type:
            return entry
    return None


def _build_evidence(member, check_states):
    """逐维度给出证据等级；未由 check 支撑的维度一律 ``UNVERIFIED`` + declarationOnly。"""
    identity = _check_evidence(check_states, "identity")
    model_check = _check_evidence(check_states, "model")
    runtime = _check_evidence(check_states, "runtime")

    identity_level = (
        identity["evidenceLevel"]
        if identity is not None and identity["satisfied"]
        else EVIDENCE_UNVERIFIED
    )
    model_level = (
        model_check["evidenceLevel"]
        if model_check is not None and model_check["satisfied"]
        else EVIDENCE_UNVERIFIED
    )
    runtime_level = (
        runtime["evidenceLevel"]
        if runtime is not None and runtime["satisfied"]
        else EVIDENCE_UNVERIFIED
    )

    model_profile = member["modelProfile"]
    # 只有 OFFICIAL 级 model check 才把“实际模型”记为已观察；其余（LOCAL / SYNTHETIC）
    # 仍是声明，host-default 也永远不凭空补 provider / modelId。
    actual_model_observed = model_level == "OFFICIAL"

    return {
        "identity": _dimension(
            identity_level,
            identity_level == EVIDENCE_UNVERIFIED,
            source=(
                "checks.identity:%s" % identity["checkId"]
                if identity is not None and identity["checkId"]
                else "declaration"
            ),
            note="只读已登记的身份 check；本模块不向宿主认证，也不接管真实身份",
        ),
        "environment": _dimension(
            EVIDENCE_UNVERIFIED,
            True,
            consistencyWithMemberReference="matched",
            note="成员当前 environment 引用内部一致；这是登记声明，不是宿主实测",
        ),
        "binding": _dimension(
            EVIDENCE_UNVERIFIED,
            True,
            consistencyWithMemberReference="matched",
            note="成员当前 binding 引用内部一致；这是登记声明，不是可执行性证明",
        ),
        "model": _dimension(
            model_level,
            model_level == EVIDENCE_UNVERIFIED,
            strategy=model_profile["strategy"],
            declaredProvider=model_profile["provider"],
            declaredModelId=model_profile["modelId"],
            actualModelObserved=actual_model_observed,
            forgedModel=False,
            note="host-default 不补 provider / modelId；fixed 只透传登记声明",
        ),
        "runtime": _dimension(
            runtime_level,
            runtime_level == EVIDENCE_UNVERIFIED,
            note="只读已登记的 runtime check；本模块不启动宿主进程",
        ),
        "host": _dimension(
            EVIDENCE_UNVERIFIED,
            True,
            recordedEvidenceOnly=True,
            hostStarted=False,
            note="宿主身份未由本模块核验；不把登记声明当成真实宿主身份",
        ),
        "project": _dimension(
            EVIDENCE_UNVERIFIED,
            True,
            note="项目关联只做声明一致性核验，不代表项目级授权已生效",
        ),
        "permissionCeiling": _dimension(
            EVIDENCE_UNVERIFIED,
            True,
            notOsIsolation=True,
            notRealHostIdentity=True,
            note="permission ceiling 是声明上限，不是 OS 隔离，也不是真实宿主身份",
        ),
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def preflight_member(conn, *, registry_id, caller, request, evaluated_at,
                     required_checks=None):
    """只读判断目标候选成员当前是否具备接收任务的条件。

    返回 ``dsh-member-preflight.v1``：

    - ``schemaVersion`` / ``registry``：被复用列表服务的 registry 自述。
    - ``request``：规范化后的请求回显（含 ``normalizedAlias`` 与 ``evaluatedAt``）。
    - ``caller``：受控入口身份（``CallerContext.to_dict()``）。
    - ``decision``：``allow`` / ``deny`` / ``recheck``。
    - ``reasons``：稳定 ``{code, level, field, detail}`` 列表（固定顺序）。
    - ``member`` / ``project``：命中的成员当前投影与命中的项目关联（未命中为 ``None``）。
    - ``checks``：逐类型 check 判定结果（是否 satisfied / 绑定当前版本 / 具体 checkId）。
    - ``evidence``：逐维度证据等级；声明层一律 ``UNVERIFIED``。
    - ``claims`` / ``authority``：显式声明本模块没有做 OS 隔离验证、真实宿主身份验证、
      模型实测、宿主启动、网络 / 凭据访问，也没有改变名册权威与 DSH 真实授权门。

    受控拒绝抛既有 ``ContractError``：``invalid_task``（入参 / 载荷非法，含
    ``required_checks`` 不是完整四类必查集合）或 ``permission_denied``（身份不足）。
    SQLite 运行错误保留原类型向上传播。
    """
    # 1) 连接与注册表入参先拒绝（不执行任何查询 / 不开事务）。
    _require_connection(conn)
    _check_row_factory(conn)
    if conn.in_transaction:
        _invalid("connection", REASON_CONNECTION_IN_TRANSACTION)
    _validate_registry_id(registry_id)

    # 2) 载荷与时间入参先拒绝（含 payload 自报身份）。
    agent_id, agent_alias, normalized_alias, project_id, model_profile_id = _validate_request(request)
    evaluated = _validate_evaluated_at(evaluated_at)
    required = _validate_required_checks(required_checks)

    # 3) 身份门槛：只接受受控入口注入的 owner / reviewer 上下文。
    _require_reviewer_caller(caller)

    # 4) 复用 02B1A 只读投影：同快照 + 全体成员完整校验，损坏数据在此 fail closed。
    projection = list_registry_agents(conn, registry_id=registry_id)
    items = projection["items"]

    reasons = []
    member, identity_reason = _resolve_member(items, agent_id, normalized_alias)
    if identity_reason is not None:
        reasons.append(identity_reason)

    project = None
    check_states = []
    satisfied_by_type = {check_type: False for check_type in required}
    if member is not None:
        if not member["enabled"]:
            reasons.append(_reason("member_disabled", LEVEL_DENY, "member.enabled",
                                   "成员当前为停用状态，拒绝派发"))
        if not member["registrationComplete"]:
            reasons.append(_reason("registration_incomplete", LEVEL_DENY,
                                   "member.registrationComplete", "成员登记未完成，拒绝派发"))

        for candidate in member["projects"]:
            if candidate["projectId"] == project_id:
                project = candidate
                break
        if project is None:
            reasons.append(_reason("project_not_associated", LEVEL_DENY, "member.projects",
                                   "成员未关联请求的 projectId，拒绝派发"))
        elif not project["enabled"]:
            reasons.append(_reason("project_disabled", LEVEL_DENY, "project.enabled",
                                   "成员与该项目的关联当前为停用，拒绝派发"))

        if model_profile_id is not None and model_profile_id != member["modelProfile"]["modelProfileId"]:
            reasons.append(_reason("model_profile_mismatch", LEVEL_DENY, "request.modelProfileId",
                                   "请求的 modelProfileId 与成员当前引用不一致"))

        check_states, check_reasons, satisfied_by_type = _evaluate_checks(
            member, required, evaluated
        )
        reasons.extend(check_reasons)

    if any(reason["level"] == LEVEL_DENY for reason in reasons):
        decision = DECISION_DENY
    elif any(reason["level"] == LEVEL_RECHECK for reason in reasons):
        decision = DECISION_RECHECK
    else:
        decision = DECISION_ALLOW

    if member is None:
        evidence = None
        member_projection = None
    else:
        evidence = _build_evidence(member, check_states)
        member_projection = {
            "agentId": member["agentId"],
            "agentAlias": member["agentAlias"],
            "normalizedAlias": member["normalizedAlias"],
            "enabled": member["enabled"],
            "registrationComplete": member["registrationComplete"],
            "revision": member["revision"],
            "roles": list(member["roles"]),
            "capabilities": list(member["capabilities"]),
            "environment": dict(member["environment"]),
            "binding": dict(member["binding"]),
            "modelProfile": dict(member["modelProfile"]),
        }

    return {
        "schemaVersion": PREFLIGHT_SCHEMA_VERSION,
        "registry": dict(projection["registry"]),
        "request": {
            "agentId": agent_id,
            "agentAlias": agent_alias,
            "normalizedAlias": normalized_alias,
            "projectId": project_id,
            "modelProfileId": model_profile_id,
            "evaluatedAt": evaluated_at,
            "requiredChecks": list(required),
        },
        "caller": caller.to_dict(),
        "decision": decision,
        "reasons": reasons,
        "member": member_projection,
        "project": project,
        "checks": check_states,
        "checksSatisfied": {check_type: bool(satisfied_by_type[check_type])
                            for check_type in required},
        "evidence": evidence,
        "claims": {
            "osIsolationVerified": False,
            "realHostIdentityVerified": False,
            "realModelObserved": bool(
                evidence is not None and evidence["model"]["actualModelObserved"]
            ),
            "declarationsTreatedAsEnforcement": False,
            "executableProven": False,
        },
        "authority": {
            "wouldWrite": False,
            "wouldDispatch": False,
            "authorityUnchanged": True,
            "hostStarted": False,
            "networkAccessed": False,
            "credentialsAccessed": False,
            "realDshAuthorizationGateUnchanged": True,
            "realDshConnected": False,
        },
    }
