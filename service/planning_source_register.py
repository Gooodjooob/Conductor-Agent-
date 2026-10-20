"""D4-MVP20-019：规划来源的受控登记（候选实现）。

冻结边界：``inputs/cox-register-decision.md``（内容状态：``planned``）。

本卡只做一件事：在**调用方注入的、已显式迁移到 planning schema v1** 的 SQLite
连接上，把一份「已声明来源」登记进 ``planning_sources``，并写一条
``planning_audit``。全程不导入内容、不生成快照、不激活规划。

明确不做（一律 fail closed）：

* 从不打开数据库路径、不调用迁移、不默认建库；``connection`` 必须由调用方注入。
* 拒绝调用方已开启的事务；不 commit / rollback 调用方的事务。
* 要求外键 ON；只读检查 ``PRAGMA foreign_keys``，不自行改变连接配置。
* 不接受调用方自报编号（``planningSourceId`` / ``registeredBy`` / ``allowedRoot`` /
  ``active`` 及任何执行事实字段都属于未知字段，一律拒绝）。编号一律由本模块用
  UUID4 生成（``src-`` / ``pa-`` 前缀），**不由路径派生、不按路径 hash 生成**。
* 不写 ``planning_snapshots``，不写任何 task / attempt / event / delivery / review /
  Registry 事实，不接 CLI / HTTP，不激活规划。
* 来源可读性一律**复用** ``service.planning_preflight.preflight_source`` 的受控预检，
  本模块不另造路径读取逻辑、不弱化允许根 / 符号链接 / 普通文件 / 大小 / 权限规则。
* 预检通过只说明「本次声明的来源此刻在受控根内可读」。**没有导入内容，所以本模块
  不声称来源版本已冻结**——真正导入时必须再次预检，也不把「登记后原文更新」误当
  「自动更新快照」。

受控授权：复用 ``service.context.CallerContext`` / ``require_context``，本模块自定义
能力 ``planning.source.register``（不改全局 context 的能力常量）。该能力只表示「本受控
入口的来源登记管理授权」，是本地调用合同，**不是网络认证，也不代表真实接入已验证**。

依赖加载：只做**直接导入**（``service.context`` / ``service.planning_preflight`` /
``contracts.schemas``）。本候选不做父目录搜索、不读环境变量覆盖、不退化成本地实现；
调用方 / 测试负责把冻结的 formal 包根 ``shared/agent-orchestrator`` 放到 ``sys.path``。
"""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

# 只读 direct import：依赖加载失败即 ImportError fail closed，不静默退化。
from contracts.schemas import ContractError
from service.context import CallerContext, require_context
from service.planning_preflight import PlanningPreflightError, preflight_source

__all__ = [
    "AUDIT_ACTION",
    "AUDIT_REASON",
    "PLANNING_SOURCE_REGISTER",
    "PLANNING_SOURCE_REGISTER_ERROR_CODES",
    "REGISTER_PAYLOAD_FIELDS",
    "REGISTRABLE_SOURCE_KINDS",
    "REQUIRED_PLANNING_SCHEMA_VERSION",
    "PlanningSourceRegistrationError",
    "register_planning_source",
]


# ---------------------------------------------------------------------------
# 本卡冻结的常量（不改全局 ERROR_CODES，不改全局 context 能力表）
# ---------------------------------------------------------------------------

#: 本受控入口的来源登记管理能力；只表达本地调用合同，不代表真实接入已验证。
PLANNING_SOURCE_REGISTER = "planning.source.register"

#: payload 只允许这三个字段，且都必须是「非空字符串」。
REGISTER_PAYLOAD_FIELDS = ("projectId", "sourceKind", "path")

#: sourceKind 闭集（与冻结 schema 的 CHECK 约束同口径）。
REGISTRABLE_SOURCE_KINDS = ("product", "development-plan", "other-registered")

#: 只接受 role=owner，且 actorId 非空、entry 非空且不是 unspecified。
REQUIRED_ROLE = "owner"
UNSPECIFIED_ENTRY = "unspecified"

#: 冻结迁移把版本号写在 planning_schema_meta.version；本模块只读校验，不调用迁移。
REQUIRED_PLANNING_SCHEMA_VERSION = 1

#: 登记必须落地的两张表（外加既有的 workspace_projects 与版本标记表）。
REQUIRED_TABLES = (
    "workspace_projects",
    "planning_schema_meta",
    "planning_sources",
    "planning_audit",
)

AUDIT_ACTION = "source.register"
AUDIT_REASON = "explicit controlled source registration"

SOURCE_ID_PREFIX = "src-"
AUDIT_ID_PREFIX = "pa-"

PLANNING_SOURCE_REGISTER_ERROR_CODES = {
    "permission_denied": "受控入口身份或授权不满足来源登记要求，拒绝且不读取文件",
    "planning_project_unregistered": "项目未登记（workspace_projects 无此 projectId），拒绝登记",
    "planning_source_invalid": "来源登记入参不符合受控形态（字段 / 类型 / 受控根），拒绝继续",
    "planning_source_preflight_rejected": "来源预检结论不是 read，保留 diagnostic 且不写任何记录",
    "planning_source_registration_conflict": "同一路径已登记但来源类型或允许根不同，拒绝且零写入",
    "planning_storage_not_ready": "调用方连接未就绪（类型 / 事务 / 外键 / schema 版本），拒绝登记",
    "planning_source_registration_failed": "登记事务失败并已回滚自己开启的事务，零来源残留",
}


class PlanningSourceRegistrationError(ValueError):
    """来源登记失败：调用方必须 fail closed，不写乐观状态、不自动重试。"""

    def __init__(self, code, message=None, details=None):
        if code not in PLANNING_SOURCE_REGISTER_ERROR_CODES:
            raise ValueError("unknown planning source registration error code: %r" % (code,))
        self.code = code
        self.details = dict(details) if details else {}
        super(PlanningSourceRegistrationError, self).__init__(
            message or PLANNING_SOURCE_REGISTER_ERROR_CODES[code]
        )

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def _canonical_json(value):
    """稳定 JSON：键排序、非 ASCII 不转义、无多余空格（与契约层同口径）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 1) 受控授权（必须先于任何文件读取与数据库写入）
# ---------------------------------------------------------------------------


def _authorize(context):
    """受控授权：能力 + role=owner + actorId 非空 + entry 受控，缺一拒绝。"""
    try:
        ctx = require_context(context, PLANNING_SOURCE_REGISTER)
    except ContractError as exc:
        raise PlanningSourceRegistrationError(
            "permission_denied",
            message="受控入口授权失败，默认拒绝：%s" % (str(exc) or type(exc).__name__),
            details={
                "required": PLANNING_SOURCE_REGISTER,
                "cause": {
                    "code": getattr(exc, "code", None),
                    "details": getattr(exc, "details", {}),
                },
            },
        ) from exc

    rejected = {}
    role = getattr(ctx, "role", None)
    if role != REQUIRED_ROLE:
        rejected["role"] = role
    actor_id = getattr(ctx, "actor_id", None)
    if not isinstance(actor_id, str) or not actor_id.strip():
        rejected["actorId"] = actor_id
    entry = getattr(ctx, "entry", None)
    if (
        not isinstance(entry, str)
        or not entry.strip()
        or entry.strip().lower() == UNSPECIFIED_ENTRY
    ):
        rejected["entry"] = entry
    if rejected:
        raise PlanningSourceRegistrationError(
            "permission_denied",
            message="受控入口身份不满足来源登记要求（须 role=owner / actorId 非空 / entry 受控）",
            details={"required": PLANNING_SOURCE_REGISTER, "rejected": rejected},
        )
    return ctx


# ---------------------------------------------------------------------------
# 2) payload 与受控允许根（均不从 payload 取上限）
# ---------------------------------------------------------------------------


def _validate_payload(payload):
    """payload 只允许 projectId / sourceKind / path；未知字段整体拒绝。"""
    if not isinstance(payload, dict):
        raise PlanningSourceRegistrationError(
            "planning_source_invalid",
            message="payload 必须是对象",
            details={"type": type(payload).__name__},
        )

    unknown = sorted(set(payload) - set(REGISTER_PAYLOAD_FIELDS))
    if unknown:
        raise PlanningSourceRegistrationError(
            "planning_source_invalid",
            message="payload 含未知字段（含自报编号 / 身份 / 允许根 / 执行事实），整体拒绝",
            details={"unknownFields": unknown, "allowedFields": list(REGISTER_PAYLOAD_FIELDS)},
        )

    missing = [field for field in REGISTER_PAYLOAD_FIELDS if field not in payload]
    if missing:
        raise PlanningSourceRegistrationError(
            "planning_source_invalid",
            message="payload 缺少必填字段",
            details={"missing": missing, "allowedFields": list(REGISTER_PAYLOAD_FIELDS)},
        )

    for field in REGISTER_PAYLOAD_FIELDS:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PlanningSourceRegistrationError(
                "planning_source_invalid",
                message="payload 字段必须是非空字符串",
                details={"field": field, "type": type(value).__name__},
            )

    source_kind = payload["sourceKind"]
    if source_kind not in REGISTRABLE_SOURCE_KINDS:
        raise PlanningSourceRegistrationError(
            "planning_source_invalid",
            message="sourceKind 不在闭集内",
            details={"sourceKind": source_kind, "allowed": list(REGISTRABLE_SOURCE_KINDS)},
        )

    return payload["projectId"], source_kind, payload["path"]


def _validate_allowed_roots(allowed_roots):
    """受控允许根只来自函数参数；缺配置或不合法一律拒绝，不从 payload 取。"""
    if not isinstance(allowed_roots, (list, tuple)) or not allowed_roots:
        raise PlanningSourceRegistrationError(
            "planning_source_invalid",
            message="缺少受控允许根（必须由受控函数参数注入，不来自 payload）",
            details={"allowedRoots": allowed_roots},
        )
    roots = []
    for entry in allowed_roots:
        if not isinstance(entry, str) or not entry.strip() or not entry.startswith("/"):
            raise PlanningSourceRegistrationError(
                "planning_source_invalid",
                message="受控允许根必须是非空绝对路径字符串",
                details={"entry": entry},
            )
        roots.append(entry)
    return roots


# ---------------------------------------------------------------------------
# 3) 存储就绪（只读检查：类型 / 调用方事务 / 外键 / schema 版本）
# ---------------------------------------------------------------------------


def _require_storage(connection):
    """要求调用方提供「已显式迁移到 planning schema v1」且无未提交事务的连接。"""
    if not isinstance(connection, sqlite3.Connection):
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="必须传入调用方 sqlite3.Connection；本模块不打开数据库路径、不建库",
            details={"type": type(connection).__name__},
        )
    if connection.in_transaction:
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="调用方已有未提交事务，拒绝登记，也不代其 commit / rollback",
            details={"reason": "caller_transaction_open"},
        )

    try:
        row = connection.execute("PRAGMA foreign_keys").fetchone()
    except sqlite3.DatabaseError as exc:
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="无法读取外键开关状态：%s" % exc,
            details={"reason": "foreign_keys_unreadable"},
        ) from exc
    if row is None or int(row[0]) != 1:
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="SQLite 外键未开启（本模块不自行改变连接配置），拒绝登记",
            details={"reason": "foreign_keys_off"},
        )

    try:
        names = {
            str(item[0])
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except sqlite3.DatabaseError as exc:
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="无法读取 schema 对象：%s" % exc,
            details={"reason": "schema_unreadable"},
        ) from exc

    missing = sorted(set(REQUIRED_TABLES) - names)
    if missing:
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="planning schema 未就绪（缺表），本模块不调用迁移、不建库",
            details={"reason": "planning_schema_absent", "missing": missing},
        )

    try:
        rows = connection.execute(
            "SELECT singleton, version FROM planning_schema_meta"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="planning_schema_meta 不是冻结标记表：%s" % exc,
            details={"reason": "planning_schema_meta_unreadable"},
        ) from exc
    if len(rows) != 1:
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="planning_schema_meta 必须恰好一行",
            details={"reason": "planning_schema_meta_row_count", "rows": len(rows)},
        )
    singleton, version = rows[0]
    if int(singleton) != 1 or int(version) != REQUIRED_PLANNING_SCHEMA_VERSION:
        raise PlanningSourceRegistrationError(
            "planning_storage_not_ready",
            message="planning schema 版本不是 v1，本模块只支持已显式迁移的 v1",
            details={
                "reason": "planning_schema_version_mismatch",
                "singleton": singleton,
                "version": version,
                "requiredVersion": REQUIRED_PLANNING_SCHEMA_VERSION,
            },
        )


# ---------------------------------------------------------------------------
# 4) 只读预检（复用冻结预检；结论不是 read 一律拒绝且不写）
# ---------------------------------------------------------------------------


def _preflight_declared_source(declared_path, roots):
    """复用 ``preflight_source``；非 read 结论保留 diagnostic 并拒绝。"""
    try:
        result = preflight_source({"path": declared_path}, roots)
    except PlanningPreflightError as exc:
        raise PlanningSourceRegistrationError(
            "planning_source_invalid",
            message="来源预检入参 / 受控允许根不合法，拒绝登记：%s" % exc,
            details={
                "cause": getattr(exc, "to_dict", lambda: {"code": type(exc).__name__})(),
                "allowedRoots": [os.path.realpath(root) for root in roots],
            },
        ) from exc

    status = result.get("status")
    if status != "read":
        raise PlanningSourceRegistrationError(
            "planning_source_preflight_rejected",
            message="来源预检结论不是 read（status=%s），拒绝登记且不写任何记录" % status,
            details={
                "declaredPath": declared_path,
                "status": status,
                # 保留预检 diagnostic 供人工核对；**不写入任何表**。
                "reason": result.get("reason"),
                "absolutePath": result.get("absolutePath"),
            },
        )
    return result


def _deepest_allowed_root(actual_path, roots):
    """从受控根里选出「最深地包含实际路径」的那一个（不把列表拼成字符串）。"""
    target = os.path.realpath(actual_path)
    best = None
    for root in roots:
        root_real = os.path.realpath(root)
        if target == root_real or target.startswith(root_real + os.sep):
            if best is None or len(root_real) > len(best):
                best = root_real
    return best


# ---------------------------------------------------------------------------
# 5) 登记事务（单个 BEGIN IMMEDIATE；审计失败整体回滚）
# ---------------------------------------------------------------------------


def _rollback(connection):
    """只回滚本模块自己开启的事务；回滚失败不掩盖原始错误。"""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def register_planning_source(connection, payload, context=None, allowed_roots=None):
    """登记一份已声明规划来源，返回 ``projectId / planningSourceId / sourceKind /
    path / replayed`` 。

    顺序固定：受控授权 -> payload / 受控根校验 -> 存储就绪 -> 只读预检 ->
    单个 ``BEGIN IMMEDIATE`` 事务（项目核验、幂等查询、插入来源 + 审计）。

    返回值只描述「本次登记事实」，**不返回可派发 / 在线 / 门禁通过等任何结论**。
    """
    ctx = _authorize(context)
    project_id, source_kind, declared_path = _validate_payload(payload)
    roots = _validate_allowed_roots(allowed_roots)
    _require_storage(connection)

    preflight = _preflight_declared_source(declared_path, roots)
    actual_path = preflight.get("absolutePath")
    if not isinstance(actual_path, str) or not actual_path.strip():
        raise PlanningSourceRegistrationError(
            "planning_source_preflight_rejected",
            message="预检结论为 read 但缺少实际路径，拒绝登记",
            details={"declaredPath": declared_path, "reason": "preflight_actual_path_missing"},
        )
    source_bytes = preflight.get("bytes")
    source_sha256 = preflight.get("sha256")
    if not isinstance(source_sha256, str) or not source_sha256:
        raise PlanningSourceRegistrationError(
            "planning_source_preflight_rejected",
            message="预检结论为 read 但缺少来源摘要，拒绝登记",
            details={"declaredPath": declared_path, "reason": "preflight_digest_missing"},
        )
    allowed_root = _deepest_allowed_root(actual_path, roots)
    if allowed_root is None:
        raise PlanningSourceRegistrationError(
            "planning_source_preflight_rejected",
            message="实际路径不在任何受控允许根内，拒绝登记",
            details={"reason": "actual_path_outside_allowed_roots", "path": actual_path},
        )

    # 身份与时间只来自受控 context / 服务时钟；synthetic 原样保留在 JSON 里。
    identity_json = _canonical_json(ctx.to_dict())
    occurred_at = datetime.now(timezone.utc).isoformat()

    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.DatabaseError as exc:
        # 未开启事务，因此这里不回滚。
        raise PlanningSourceRegistrationError(
            "planning_source_registration_failed",
            message="无法开启登记事务：%s" % exc,
            details={"reason": "begin_failed"},
        ) from exc

    try:
        project_row = connection.execute(
            "SELECT project_id FROM workspace_projects WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if project_row is None:
            raise PlanningSourceRegistrationError(
                "planning_project_unregistered",
                message="项目未登记（workspace_projects 无此 projectId），拒绝登记",
                details={"projectId": project_id},
            )

        existing = connection.execute(
            "SELECT planning_source_id, source_kind, allowed_root "
            "FROM planning_sources WHERE project_id = ? AND source_path = ?",
            (project_id, actual_path),
        ).fetchall()

        if len(existing) > 1:
            raise PlanningSourceRegistrationError(
                "planning_source_registration_conflict",
                message="同一项目同一路径出现多条登记记录，拒绝继续",
                details={"projectId": project_id, "path": actual_path, "rows": len(existing)},
            )

        if existing:
            registered_id, registered_kind, registered_root = existing[0][0], existing[0][1], existing[0][2]
            if registered_kind == source_kind and registered_root == allowed_root:
                # 幂等重放：返回原编号，不新增来源 / 审计，不改原身份与时间。
                connection.execute("COMMIT")
                return {
                    "projectId": project_id,
                    "planningSourceId": registered_id,
                    "sourceKind": source_kind,
                    "path": actual_path,
                    "replayed": True,
                }
            raise PlanningSourceRegistrationError(
                "planning_source_registration_conflict",
                message="同一路径已登记但来源类型或允许根不同，拒绝且零写入",
                details={
                    "projectId": project_id,
                    "path": actual_path,
                    "registeredSourceKind": registered_kind,
                    "requestedSourceKind": source_kind,
                    "registeredAllowedRoot": registered_root,
                    "requestedAllowedRoot": allowed_root,
                },
            )

        # 系统分配编号：UUID4 随机，非路径派生。
        planning_source_id = SOURCE_ID_PREFIX + uuid.uuid4().hex
        audit_id = AUDIT_ID_PREFIX + uuid.uuid4().hex

        # 只有参数化值，没有字符串插值。
        connection.execute(
            "INSERT INTO planning_sources ("
            "project_id, planning_source_id, source_kind, source_path, allowed_root, "
            "registered_by_json, registered_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                planning_source_id,
                source_kind,
                actual_path,
                allowed_root,
                identity_json,
                occurred_at,
            ),
        )
        detail_json = _canonical_json(
            {
                # 只含声明来源与预检字节摘要，不含正文、不含执行事实。
                "declaredSource": {
                    "projectId": project_id,
                    "sourceKind": source_kind,
                    "path": declared_path,
                },
                "preflightBytes": source_bytes,
                "preflightSha256": source_sha256,
            }
        )
        connection.execute(
            "INSERT INTO planning_audit ("
            "audit_id, project_id, planning_source_id, action, actor_json, reason, "
            "occurred_at, detail_json"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_id,
                project_id,
                planning_source_id,
                AUDIT_ACTION,
                identity_json,
                AUDIT_REASON,
                occurred_at,
                detail_json,
            ),
        )
        connection.execute("COMMIT")
    except PlanningSourceRegistrationError:
        _rollback(connection)
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise PlanningSourceRegistrationError(
            "planning_source_registration_failed",
            message="登记事务失败并已回滚：%s" % exc,
            details={"reason": "transaction_failed", "error": type(exc).__name__},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 任何意外失败同样要求零残留
        _rollback(connection)
        raise PlanningSourceRegistrationError(
            "planning_source_registration_failed",
            message="登记出现意外失败并已回滚：%s" % exc,
            details={"reason": "unexpected_failure", "error": type(exc).__name__},
        ) from exc

    return {
        "projectId": project_id,
        "planningSourceId": planning_source_id,
        "sourceKind": source_kind,
        "path": actual_path,
        "replayed": False,
    }
