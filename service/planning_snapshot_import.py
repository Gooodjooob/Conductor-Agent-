"""D4-MVP20-023：首版受控规划快照保存候选（``planning.snapshot.import``）。

冻结边界：``inputs/cox-first-import-decision.md``（内容状态：``planned``）。

本卡只做一件事：在**调用方注入的、已显式迁移到 planning schema v1** 的 SQLite
连接上，把一次「受控调用者提交的显式结构化规划载荷」保存为**首版候选快照**
（``planning_snapshots`` 一条 + ``planning_audit`` 一条），全程：

* 不解析 Markdown、不证明章节标题来自原文（后续资料转换卡的范围）；
* 不追加新版本（``snapshot_revision`` 恒为 1）、不激活规划、不新增任何激活指针；
* 不写执行事实（task / attempt / event / delivery / review / Registry）；
* 不调用迁移、不打开数据库路径、不自动建表、不改 PRAGMA、不 commit / rollback
  调用方的事务、不使用 ``executescript``、不自动重试；
* 完整 MVP / A 阶段**未完成**，``planStatus`` 里的 ``active`` / ``validated``
  只是资料标签，不表示激活或验收。

受控授权：复用 ``service.context.CallerContext`` / ``require_context``，本模块自定义
能力 ``planning.snapshot.import``（不改全局能力常量）。该能力只表示「本受控入口的
快照保存管理授权」，是本地调用合同，**不是网络认证，也不代表真实接入已验证**。

依赖加载：只做**直接导入**（``contracts.planning_structure`` /
``contracts.planning_digest`` / ``service.planning_preflight`` /
``service.context`` / ``contracts.schemas``）。本候选不做父目录搜索、不读环境变量
覆盖、不退化成本地实现；调用方 / 测试负责把冻结的 formal 包根
``shared/agent-orchestrator`` 放到 ``sys.path``。
"""

from __future__ import annotations

import copy
import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone

# 只读 direct import：依赖加载失败即 ImportError fail closed。
from contracts.planning_digest import PARTICIPATING_FIELDS, PlanningDigestError
from contracts.planning_structure import validate_planning_structure
from contracts.schemas import ContractError
from service.context import CallerContext, require_context
from service.planning_preflight import PlanningPreflightError, preflight_source

__all__ = [
    "AUDIT_ACTION",
    "AUDIT_REASON",
    "FIRST_SNAPSHOT_REVISION",
    "PLANNING_SNAPSHOT_IMPORT",
    "PLANNING_SNAPSHOT_IMPORT_ERROR_CODES",
    "REQUIRED_PLANNING_SCHEMA_VERSION",
    "REQUIRED_TABLES",
    "SNAPSHOT_STATUS",
    "PlanningSnapshotImportError",
    "import_planning_snapshot",
]


# ---------------------------------------------------------------------------
# 本卡冻结的常量（不改全局 ERROR_CODES，不改全局 context 能力表）
# ---------------------------------------------------------------------------

#: 本受控入口的快照保存管理能力；只表达本地调用合同，不代表真实接入已验证。
PLANNING_SNAPSHOT_IMPORT = "planning.snapshot.import"

#: 只接受 role=owner，且 actorId 非空、entry 非空且不是 unspecified。
REQUIRED_ROLE = "owner"
UNSPECIFIED_ENTRY = "unspecified"

#: 冻结迁移把版本号写在 planning_schema_meta.version；本模块只读校验，不调用迁移。
REQUIRED_PLANNING_SCHEMA_VERSION = 1

#: 存储就绪必须存在的表（本模块只读检查，不建表、不迁移）。
REQUIRED_TABLES = (
    "workspace_projects",
    "planning_schema_meta",
    "planning_sources",
    "planning_snapshots",
    "planning_audit",
)

AUDIT_ACTION = "snapshot.import"
AUDIT_REASON = "explicit controlled first snapshot import"

SNAPSHOT_ID_PREFIX = "snap-"
AUDIT_ID_PREFIX = "pa-"

#: 本首版切片只保存候选快照，且只保存首版（revision 恒为 1，非 bool）。
SNAPSHOT_STATUS = "candidate"
FIRST_SNAPSHOT_REVISION = 1

#: 顶层受控元数据字段：摘要合同允许它们出现在顶层，但**本切片一律拒绝自报**。
CONTROLLED_METADATA_FIELDS = (
    "canonicalDigest",
    "snapshotId",
    "snapshotRevision",
    "importedAt",
    "importedBy",
)

_PARTICIPATING = frozenset(PARTICIPATING_FIELDS)
_CONTROLLED_METADATA = frozenset(CONTROLLED_METADATA_FIELDS)

PLANNING_SNAPSHOT_IMPORT_ERROR_CODES = {
    "permission_denied": "受控入口身份或授权不满足快照保存要求，拒绝且不读取文件、不写库",
    "planning_storage_not_ready": "调用方连接未就绪（类型 / 事务 / 外键 / schema 版本 / 缺表），拒绝保存",
    "planning_snapshot_invalid": "快照载荷不符合受控形态（顶层字段 / 受控元数据自报 / 基础类型 / 允许根），拒绝继续",
    "planning_project_unregistered": "项目未登记（workspace_projects 无此 projectId），拒绝保存",
    "planning_source_unregistered": "来源未登记（planning_sources 无此 project+planningSourceId），拒绝保存且不按路径猜来源",
    "planning_source_mismatch": "快照声明与已登记来源不一致（kind / path / name / 允许根），拒绝保存",
    "planning_source_preflight_rejected": "来源预检结论不是 read（非读取类拒绝），保留 diagnostic 且零写入",
    "planning_source_drift": "来源读取期间字节 / 摘要漂移或与声明不一致，本批拒绝且零写入",
    "planning_snapshot_conflict": "同 project/source 已有快照且内容不同（首版切片不追加 revision），旧版不动且零写入",
    "planning_snapshot_import_failed": "快照保存事务失败并已回滚自己开启的事务，零快照残留",
}

#: 已通过规划模块的稳定原 code：直接沿用，不另造同义码、不改全局枚举。
_PASS_THROUGH_ERROR_CODES = frozenset(
    (
        "planning_digest_invalid",
        "planning_digest_self_reference",
        "planning_execution_fact_forbidden",
        "planning_structure_invalid",
        "planning_cross_project_reference",
        "planning_mapping_invalid",
        "planning_mapping_ambiguous",
        "planning_preflight_not_configured",
        "planning_source_preflight_invalid",
    )
)

_ALLOWED_ERROR_CODES = frozenset(PLANNING_SNAPSHOT_IMPORT_ERROR_CODES) | _PASS_THROUGH_ERROR_CODES


class PlanningSnapshotImportError(ValueError):
    """快照保存失败：调用方必须 fail closed，不写乐观状态、不自动重试。"""

    def __init__(self, code, message=None, details=None):
        if code not in _ALLOWED_ERROR_CODES:
            raise ValueError("unknown planning snapshot import error code: %r" % (code,))
        self.code = code
        self.details = dict(details) if details else {}
        super(PlanningSnapshotImportError, self).__init__(
            message or PLANNING_SNAPSHOT_IMPORT_ERROR_CODES.get(code) or code
        )

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def _canonical_json(value):
    """稳定 JSON：键排序、非 ASCII 不转义、无多余空格（与契约层同口径）。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _wrap_formal_error(exc):
    """沿用已通过规划异常的原 code，并包住 details（不回显正文 / 标题 / 内容）。"""
    raw_code = getattr(exc, "code", None)
    code = raw_code if isinstance(raw_code, str) and raw_code in _ALLOWED_ERROR_CODES else (
        "planning_snapshot_invalid"
    )
    message = getattr(exc, "message", None) or str(exc)
    return PlanningSnapshotImportError(
        code,
        message=message,
        details={
            "cause": {
                "code": raw_code,
                "type": type(exc).__name__,
                "details": dict(getattr(exc, "details", {}) or {}),
            }
        },
    )


# ---------------------------------------------------------------------------
# 1) 受控授权（必须先于任何文件读取与数据库查询 / 写入）
# ---------------------------------------------------------------------------


def _authorize(context):
    """受控授权：能力 + role=owner + actorId 非空 + entry 受控，缺一拒绝。"""
    if context is None or not isinstance(context, CallerContext):
        raise PlanningSnapshotImportError(
            "permission_denied",
            message="缺少受控入口注入的调用者身份（None / dict / 非 CallerContext 一律拒绝）",
            details={
                "required": PLANNING_SNAPSHOT_IMPORT,
                "type": None if context is None else type(context).__name__,
            },
        )

    try:
        ctx = require_context(context, PLANNING_SNAPSHOT_IMPORT)
    except ContractError as exc:
        raise PlanningSnapshotImportError(
            "permission_denied",
            message="受控入口授权失败，默认拒绝：%s" % (str(exc) or type(exc).__name__),
            details={
                "required": PLANNING_SNAPSHOT_IMPORT,
                "cause": {
                    "code": getattr(exc, "code", None),
                    "details": dict(getattr(exc, "details", {}) or {}),
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
        raise PlanningSnapshotImportError(
            "permission_denied",
            message="受控入口身份不满足快照保存要求（须 role=owner / actorId 非空 / entry 受控）",
            details={"required": PLANNING_SNAPSHOT_IMPORT, "rejected": rejected},
        )
    return ctx


# ---------------------------------------------------------------------------
# 2) 载荷基础校验（只收 PARTICIPATING_FIELDS；受控元数据与未知字段一律拒绝）
# ---------------------------------------------------------------------------


def _validate_snapshot_payload(snapshot):
    """校验顶层形态并返回**独立深拷贝**载荷（不修改调用方对象）。

    只做本切片必需的基础校验：对象、顶层字段白名单（受控元数据自报整体拒绝）、
    受控身份字段与 ``sourceFile`` 的 name / path 可用。其余字段取值 / 引用关系 /
    运行事实一律**沿用已通过摘要与结构校验**（不复制、不弱化规则）。
    """
    if not isinstance(snapshot, dict):
        raise PlanningSnapshotImportError(
            "planning_snapshot_invalid",
            message="snapshot 必须是受控结构化载荷对象",
            details={"type": type(snapshot).__name__},
        )

    try:
        payload = copy.deepcopy(snapshot)
    except Exception as exc:  # noqa: BLE001 - 复制失败同样要求 fail closed
        raise PlanningSnapshotImportError(
            "planning_snapshot_invalid",
            message="快照无法复制为独立载荷，拒绝继续",
            details={"reason": "payload_copy_failed", "error": type(exc).__name__},
        ) from exc

    unknown = []
    for key in payload:
        if not isinstance(key, str):
            unknown.append("<non-string-key:%s>" % type(key).__name__)
        elif key not in _PARTICIPATING:
            unknown.append(key)
    if unknown:
        raise PlanningSnapshotImportError(
            "planning_snapshot_invalid",
            message=(
                "快照顶层字段只能是 planning_digest.PARTICIPATING_FIELDS；"
                "受控元数据自报与未知字段一律拒绝"
            ),
            details={
                "unknownFields": sorted(unknown),
                "rejectedMetadataFields": sorted(set(unknown) & _CONTROLLED_METADATA),
                "allowedFields": list(PARTICIPATING_FIELDS),
            },
        )

    for field in ("projectId", "planningSourceId", "sourceKind"):
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PlanningSnapshotImportError(
                "planning_snapshot_invalid",
                message="快照缺少可用的受控身份字段（必须是非空字符串）",
                details={"field": field, "type": type(value).__name__},
            )

    source_file = payload.get("sourceFile")
    if not isinstance(source_file, dict):
        raise PlanningSnapshotImportError(
            "planning_snapshot_invalid",
            message="快照 sourceFile 必须是对象",
            details={"field": "sourceFile", "type": type(source_file).__name__},
        )
    for field in ("name", "path"):
        value = source_file.get(field)
        if not isinstance(value, str) or not value.strip():
            raise PlanningSnapshotImportError(
                "planning_snapshot_invalid",
                message="快照 sourceFile.name / sourceFile.path 必须是非空字符串",
                details={"field": "sourceFile.%s" % field, "type": type(value).__name__},
            )
    return payload


def _validate_allowed_roots(allowed_roots):
    """受控允许根只来自函数参数；缺配置或不合法一律拒绝，绝不从 snapshot 取。"""
    if not isinstance(allowed_roots, (list, tuple)) or not allowed_roots:
        raise PlanningSnapshotImportError(
            "planning_snapshot_invalid",
            message="缺少受控允许根（必须由受控函数参数冻结，不能来自 snapshot）",
            details={"allowedRoots": allowed_roots},
        )
    roots = []
    for entry in allowed_roots:
        if not isinstance(entry, str) or not entry.strip() or not entry.startswith("/"):
            raise PlanningSnapshotImportError(
                "planning_snapshot_invalid",
                message="受控允许根本身必须是非空绝对路径字符串",
                details={"entry": entry},
            )
        roots.append(entry)
    return roots


# ---------------------------------------------------------------------------
# 3) 存储就绪（只读检查：类型 / 调用方事务 / 外键 / schema 版本 / 缺表）
# ---------------------------------------------------------------------------


def _require_storage(connection):
    """要求调用方提供「已显式迁移到 planning schema v1」且无未提交事务的连接。"""
    if not isinstance(connection, sqlite3.Connection):
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="必须传入调用方 sqlite3.Connection；本模块不打开数据库路径、不建库",
            details={"type": type(connection).__name__},
        )
    if connection.in_transaction:
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="调用方已有未提交事务，拒绝保存，也不代其 commit / rollback",
            details={"reason": "caller_transaction_open"},
        )

    try:
        row = connection.execute("PRAGMA foreign_keys").fetchone()
    except sqlite3.Error as exc:
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="无法读取外键开关状态：%s" % exc,
            details={"reason": "foreign_keys_unreadable"},
        ) from exc
    if row is None or int(row[0]) != 1:
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="SQLite 外键未开启（本模块不自行改变连接配置），拒绝保存",
            details={"reason": "foreign_keys_off"},
        )

    try:
        names = {
            str(item[0])
            for item in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except sqlite3.Error as exc:
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="无法读取 schema 对象：%s" % exc,
            details={"reason": "schema_unreadable"},
        ) from exc

    missing = sorted(set(REQUIRED_TABLES) - names)
    if missing:
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="planning schema 未就绪（缺表），本模块不调用迁移、不建表",
            details={"reason": "planning_schema_absent", "missing": missing},
        )

    try:
        rows = connection.execute(
            "SELECT singleton, version FROM planning_schema_meta"
        ).fetchall()
    except sqlite3.Error as exc:
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="planning_schema_meta 不是冻结标记表：%s" % exc,
            details={"reason": "planning_schema_meta_unreadable"},
        ) from exc
    if len(rows) != 1:
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="planning_schema_meta 必须恰好一行",
            details={"reason": "planning_schema_meta_row_count", "rows": len(rows)},
        )
    singleton, version = rows[0]
    if int(singleton) != 1 or int(version) != REQUIRED_PLANNING_SCHEMA_VERSION:
        raise PlanningSnapshotImportError(
            "planning_storage_not_ready",
            message="planning schema 版本不是 v1，本模块只支持已显式迁移的 v1",
            details={
                "reason": "planning_schema_version_mismatch",
                "singleton": singleton,
                "version": version,
                "requiredVersion": REQUIRED_PLANNING_SCHEMA_VERSION,
            },
        )


def _rollback(connection):
    """只回滚本模块自己开启的事务；回滚失败不掩盖原始错误。"""
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# 4) 受控只读预检（复用冻结预检，不另造路径读取逻辑）
# ---------------------------------------------------------------------------


def _preflight(path, allowed_root, expected_bytes=None, expected_sha256=None):
    """单次受控只读预检；预检入参 / 配置错误沿用其原 code 并包 details。"""
    try:
        return preflight_source(
            {"path": path},
            [allowed_root],
            expected_bytes=expected_bytes,
            expected_sha256=expected_sha256,
        )
    except PlanningPreflightError as exc:
        raise _wrap_formal_error(exc) from exc


def _narrowest_common_root(registered_root, roots):
    """注册允许根与本次受控参数的**共同限制**（只收窄，绝不放宽注册根）。

    注册根必须仍落在某个受控根内；此时两者的交集就是更窄的注册根本身。

    包含判断按**路径组件**（``os.path.commonpath``）而非字符串前缀拼接：
    受控根为 ``"/"`` 时其规范化结果就是 ``"/"``，因此任何绝对注册根都被正常覆盖
    （旧实现拼成 ``"//"`` 会误拒）；而 ``/a/bc`` 这类「同前缀但非祖先」的目录
    不会被误判为包含，也不会放宽到注册根。

    注册根不被任何受控根覆盖时返回 ``None``（调用方拒绝，不放宽到注册根）。
    """
    if not isinstance(registered_root, str) or not registered_root.strip():
        return None
    if not registered_root.startswith("/"):
        return None
    registered_real = os.path.realpath(registered_root)
    for root in roots:
        root_real = os.path.realpath(root)
        try:
            common = os.path.commonpath([registered_real, root_real])
        except ValueError:
            # 绝对 / 相对混用等不可比较路径：不放宽，跳过该受控根。
            continue
        if common == root_real:
            return registered_real
    return None


def _require_read(result, stage):
    """把非 read 的预检结论转成确定错误码：漂移优先，其余归为非读取类拒绝。"""
    status = result.get("status")
    if status == "drift":
        raise PlanningSnapshotImportError(
            "planning_source_drift",
            message="来源在受控读取期间字节 / 摘要发生漂移（或与已登记期望不一致），本批拒绝",
            details={
                "stage": stage,
                "reason": result.get("reason"),
                "drift": result.get("drift"),
                "absolutePath": result.get("absolutePath"),
            },
        )
    if status != "read":
        raise PlanningSnapshotImportError(
            "planning_source_preflight_rejected",
            message="来源预检结论不是 read（status=%s），拒绝保存且零写入" % status,
            details={
                "stage": stage,
                "status": status,
                "reason": result.get("reason"),
                "claimedPath": result.get("claimedPath"),
                "absolutePath": result.get("absolutePath"),
            },
        )
    return result


# ---------------------------------------------------------------------------
# 5) 事务内步骤：项目 / 来源核验 -> 结构校验 -> 两次对拍 -> 幂等 / 冲突 -> 插入
# ---------------------------------------------------------------------------


def _load_registered_source(connection, snapshot):
    """事务内按 (project_id, planning_source_id) 精确取来源；不跨项目、不按路径猜。"""
    project_id = snapshot["projectId"]
    planning_source_id = snapshot["planningSourceId"]

    project_row = connection.execute(
        "SELECT project_id FROM workspace_projects WHERE project_id = ?",
        (project_id,),
    ).fetchone()
    if project_row is None:
        raise PlanningSnapshotImportError(
            "planning_project_unregistered",
            message="项目未登记（workspace_projects 无此 projectId），拒绝保存",
            details={"projectId": project_id},
        )

    rows = connection.execute(
        "SELECT source_kind, source_path, allowed_root FROM planning_sources "
        "WHERE project_id = ? AND planning_source_id = ?",
        (project_id, planning_source_id),
    ).fetchall()
    if not rows:
        raise PlanningSnapshotImportError(
            "planning_source_unregistered",
            message=(
                "来源未登记（planning_sources 无此 project+planningSourceId），"
                "拒绝保存且不按路径猜来源"
            ),
            details={"projectId": project_id, "planningSourceId": planning_source_id},
        )
    if len(rows) > 1:
        raise PlanningSnapshotImportError(
            "planning_source_unregistered",
            message="同一 project+planningSourceId 出现多条来源登记，拒绝继续",
            details={
                "projectId": project_id,
                "planningSourceId": planning_source_id,
                "rows": len(rows),
            },
        )
    source_kind, source_path, registered_root = rows[0]
    # 已登记项目身份取自库内行（不信任 snapshot 自报的那个字符串本身）。
    return project_row[0], source_kind, source_path, registered_root


def _require_declaration_matches_registration(snapshot, source_kind, source_path,
                                              registered_root, roots):
    """快照声明必须与已登记来源一致；注册允许根必须仍被本次受控根覆盖。"""
    source_file = snapshot["sourceFile"]

    if snapshot["sourceKind"] != source_kind:
        raise PlanningSnapshotImportError(
            "planning_source_mismatch",
            message="快照 sourceKind 与已登记来源类型不一致，拒绝保存",
            details={
                "reason": "source_kind_mismatch",
                "registeredSourceKind": source_kind,
                "declaredSourceKind": snapshot["sourceKind"],
            },
        )
    if source_file["path"] != source_path:
        raise PlanningSnapshotImportError(
            "planning_source_mismatch",
            message="快照 sourceFile.path 与已登记来源路径不一致，拒绝保存",
            details={
                "reason": "source_path_mismatch",
                "registeredPath": source_path,
                "declaredPath": source_file["path"],
            },
        )
    registered_name = os.path.basename(source_path)
    if source_file["name"] != registered_name:
        raise PlanningSnapshotImportError(
            "planning_source_mismatch",
            message="快照 sourceFile.name 与已登记来源路径的 basename 不一致，拒绝保存",
            details={
                "reason": "source_name_mismatch",
                "registeredName": registered_name,
                "declaredName": source_file["name"],
            },
        )

    common_root = _narrowest_common_root(registered_root, roots)
    if common_root is None:
        raise PlanningSnapshotImportError(
            "planning_source_mismatch",
            message=(
                "已登记来源的允许根不在本次受控允许根覆盖内，拒绝保存"
                "（不得放宽注册根）"
            ),
            details={
                "reason": "registered_root_not_covered",
                "registeredAllowedRoot": registered_root,
                "controlledRoots": [os.path.realpath(root) for root in roots],
            },
        )
    return common_root


def _read_source_twice(source_path, common_root, source_file):
    """两次来源只读对拍：第一次用声明值，第二次用第一次实际字节 / SHA。

    两次都必须稳定为 ``read``；任何漂移先拒绝本批（优先于幂等与内容冲突判定），
    不把漂移改判成版本更新。
    """
    first = _preflight(
        source_path,
        common_root,
        expected_bytes=source_file.get("bytes"),
        expected_sha256=source_file.get("sha256"),
    )
    _require_read(first, "declared_expectation")

    second = _preflight(
        source_path,
        common_root,
        expected_bytes=first.get("bytes"),
        expected_sha256=first.get("sha256"),
    )
    _require_read(second, "first_read_readback")

    if (second.get("bytes"), second.get("sha256")) != (first.get("bytes"), first.get("sha256")):
        raise PlanningSnapshotImportError(
            "planning_source_drift",
            message="两次受控读取的实际字节 / SHA 不一致，本批拒绝",
            details={
                "stage": "two_read_comparison",
                "first": {"bytes": first.get("bytes"), "sha256": first.get("sha256")},
                "second": {"bytes": second.get("bytes"), "sha256": second.get("sha256")},
            },
        )
    return first


def _load_existing_snapshots(connection, project_id, planning_source_id):
    return connection.execute(
        "SELECT snapshot_id, snapshot_revision, status, canonical_digest, "
        "document_version, source_sha256, source_bytes "
        "FROM planning_snapshots WHERE project_id = ? AND planning_source_id = ?",
        (project_id, planning_source_id),
    ).fetchall()


def _resolve_replay_or_conflict(existing, canonical_digest, project_id,
                                planning_source_id, incoming_version, incoming_sha256):
    """幂等重放或明确冲突；两者都在查询阶段定性，不写任何记录。

    返回重放结果 dict；无任何既有快照时返回 ``None``（调用方可继续首版插入）。
    冲突是结构化拒绝：``details.reason`` 说明本首版切片为何不追加新版本。
    """
    if not existing:
        return None

    for row in existing:
        if row[3] == canonical_digest:
            return {
                "projectId": project_id,
                "planningSourceId": planning_source_id,
                "snapshotId": row[0],
                "snapshotRevision": row[1],
                "status": row[2],
                "canonicalDigest": canonical_digest,
                "replayed": True,
                "wouldActivate": False,
            }

    same_version = any(
        row[4] == incoming_version and row[5] != incoming_sha256 for row in existing
    )
    reason = (
        "same_document_version_different_bytes"
        if same_version
        else "additional_revision_not_in_this_slice"
    )
    raise PlanningSnapshotImportError(
        "planning_snapshot_conflict",
        message=(
            "同 project/source 已有快照且内容不同：本首版切片不追加新版本，"
            "旧记录保持不变且零写入"
        ),
        details={
            "projectId": project_id,
            "planningSourceId": planning_source_id,
            "reason": reason,
            "canonicalDigest": canonical_digest,
            "existingSnapshotIds": [row[0] for row in existing],
            "existingRevisions": [row[1] for row in existing],
            "existingDocumentVersions": [row[4] for row in existing],
            "existingSourceSha256": [row[5] for row in existing],
            "existingSourceBytes": [row[6] for row in existing],
            "incomingDocumentVersion": incoming_version,
            "incomingSourceSha256": incoming_sha256,
        },
    )


def _insert_candidate(connection, snapshot, ctx, canonical_digest, diagnostics,
                      document_version, source_bytes, source_sha256):
    """只 INSERT 一条快照 + 一条审计；返回本次候选保存结果（不含任何激活结论）。"""
    project_id = snapshot["projectId"]
    planning_source_id = snapshot["planningSourceId"]

    # 系统分配编号：UUID4 随机，不由路径 / 内容派生。
    snapshot_id = SNAPSHOT_ID_PREFIX + uuid.uuid4().hex
    audit_id = AUDIT_ID_PREFIX + uuid.uuid4().hex
    snapshot_revision = FIRST_SNAPSHOT_REVISION
    imported_at = datetime.now(timezone.utc).isoformat()
    identity = ctx.to_dict()
    imported_by_json = _canonical_json(identity)

    stored_payload = dict(snapshot)
    stored_payload["snapshotId"] = snapshot_id
    stored_payload["snapshotRevision"] = snapshot_revision
    stored_payload["importedAt"] = imported_at
    stored_payload["importedBy"] = identity
    stored_payload["canonicalDigest"] = canonical_digest
    payload_json = _canonical_json(stored_payload)

    detail_json = _canonical_json(
        {
            # 只含非敏感摘要：编号 / 版本 / 摘要 / 实际来源字节与 SHA / 诊断数量。
            "snapshotId": snapshot_id,
            "snapshotRevision": snapshot_revision,
            "canonicalDigest": canonical_digest,
            "documentVersion": document_version,
            "sourceBytes": source_bytes,
            "sourceSha256": source_sha256,
            "diagnosticsCount": len(diagnostics),
        }
    )

    connection.execute(
        "INSERT INTO planning_snapshots ("
        "snapshot_id, project_id, planning_source_id, snapshot_revision, "
        "document_version, source_sha256, source_bytes, canonical_digest, "
        "payload_json, imported_by_json, imported_at, status"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            snapshot_id,
            project_id,
            planning_source_id,
            snapshot_revision,
            document_version,
            source_sha256,
            source_bytes,
            canonical_digest,
            payload_json,
            imported_by_json,
            imported_at,
            SNAPSHOT_STATUS,
        ),
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
            imported_by_json,
            AUDIT_REASON,
            imported_at,
            detail_json,
        ),
    )

    return {
        "projectId": project_id,
        "planningSourceId": planning_source_id,
        "snapshotId": snapshot_id,
        "snapshotRevision": snapshot_revision,
        "status": SNAPSHOT_STATUS,
        "canonicalDigest": canonical_digest,
        "replayed": False,
        "diagnostics": list(diagnostics),
        "wouldActivate": False,
    }


def _import_within_transaction(connection, snapshot, ctx, roots):
    registered_project_id, source_kind, source_path, registered_root = (
        _load_registered_source(connection, snapshot)
    )
    common_root = _require_declaration_matches_registration(
        snapshot, source_kind, source_path, registered_root, roots
    )

    # 结构校验复用已通过函数；返回独立深拷贝载荷 / 摘要 / 缺映射诊断。
    try:
        validated = validate_planning_structure(
            snapshot,
            expected_project_id=registered_project_id,
            expected_planning_source_id=snapshot["planningSourceId"],
        )
    except PlanningDigestError as exc:
        raise _wrap_formal_error(exc) from exc

    source_file = snapshot["sourceFile"]
    first = _read_source_twice(source_path, common_root, source_file)

    canonical_digest = validated["canonicalDigest"]
    diagnostics = validated["diagnostics"]
    document_version = source_file["documentVersion"]

    existing = _load_existing_snapshots(
        connection, snapshot["projectId"], snapshot["planningSourceId"]
    )
    replayed = _resolve_replay_or_conflict(
        existing,
        canonical_digest,
        snapshot["projectId"],
        snapshot["planningSourceId"],
        document_version,
        first.get("sha256"),
    )
    if replayed is not None:
        # 幂等重放：无新快照 / 无新审计，不改原身份与时间。
        replayed["diagnostics"] = list(diagnostics)
        return replayed

    return _insert_candidate(
        connection,
        validated["payload"],
        ctx,
        canonical_digest,
        diagnostics,
        document_version,
        first.get("bytes"),
        first.get("sha256"),
    )


# ---------------------------------------------------------------------------
# 6) 公开入口
# ---------------------------------------------------------------------------


def import_planning_snapshot(connection, snapshot, context=None, allowed_roots=None):
    """把一次受控结构化规划载荷保存为**首版候选快照**（候选切片，不是完整导入）。

    顺序固定：受控授权 -> 载荷基础校验 -> 受控允许根 -> 存储就绪 ->
    单个 ``BEGIN IMMEDIATE`` 自有事务（项目 / 来源核验、声明一致性、结构校验、
    两次来源只读对拍、幂等 / 冲突判定、插入快照 + 审计）。

    返回值只描述「本次候选保存事实」：``projectId`` / ``planningSourceId`` /
    ``snapshotId`` / ``snapshotRevision`` / ``status`` / ``canonicalDigest`` /
    ``replayed`` / ``diagnostics`` / ``wouldActivate(false)``。
    **不返回激活 / 验收 / 可派发等任何结论**。
    """
    ctx = _authorize(context)
    payload = _validate_snapshot_payload(snapshot)
    roots = _validate_allowed_roots(allowed_roots)
    _require_storage(connection)

    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        # 未开启事务，因此这里不回滚。
        raise PlanningSnapshotImportError(
            "planning_snapshot_import_failed",
            message="无法开启快照保存事务：%s" % exc,
            details={"reason": "begin_failed"},
        ) from exc

    try:
        result = _import_within_transaction(connection, payload, ctx, roots)
        connection.execute("COMMIT")
    except PlanningSnapshotImportError:
        # 含受控冲突：结构化拒绝且零写入（不写审计噪音）。
        _rollback(connection)
        raise
    except sqlite3.Error as exc:
        _rollback(connection)
        raise PlanningSnapshotImportError(
            "planning_snapshot_import_failed",
            message="快照保存事务失败并已回滚：%s" % exc,
            details={"reason": "transaction_failed", "error": type(exc).__name__},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 任何意外失败同样要求零残留
        _rollback(connection)
        raise PlanningSnapshotImportError(
            "planning_snapshot_import_failed",
            message="快照保存出现意外失败并已回滚：%s" % exc,
            details={"reason": "unexpected_failure", "error": type(exc).__name__},
        ) from exc
    return result
