"""D4-MVP20-028：受控规划 revision 追加保存接线候选（planned）。

冻结边界（唯一）：``inputs/cox-revision-save-decision.md``（内容状态：``planned``）。

本卡只做一件事：在**调用方注入的、已显式迁移到 planning schema v1** 的 SQLite
连接上，把一次受控结构化规划载荷按**候选 revision** 语义保存：

* 复用正式首版服务 ``service.planning_snapshot_import`` 的受控授权、存储就绪、
  载荷 / 项目 / 来源 / 允许根 / 预检 / 结构 / 摘要校验与冻结 helper（直接导入，
  不复制规则、不弱化，**不调用其首版写事务入口**）；
* 复用 ``contracts.planning_revision.decide_planning_revision`` 做
  ``create_first`` / ``replayed`` / ``append_candidate`` 判定，``append`` 的
  ``snapshotRevision`` 取**既有最大值 + 1**（不是 ``len + 1``）；
* 只 INSERT ``planning_snapshots`` + ``planning_audit`` 各一条（同一事务原子提交），
  ``status = candidate``、``wouldActivate = false``；同一 ``canonicalDigest`` 重放原
  身份且零新增；同 ``documentVersion`` 不同字节 / SHA 硬拒（声明不能豁免）。

明确不做（本卡边界）：

* 不 connect、不迁移、不建表、不改 PRAGMA、不自动重试、不 commit / rollback 调用方
  的事务；拒调用方已有未提交事务；
* 不追加首版、不改旧行、不加激活指针、不激活规划、不写执行事实
  （task / attempt / event / delivery / review / Registry）；
* 不接 CLI / HTTP，不做资料转换，不证明原文版本 / 章节真实存在（另卡）。

漂移优先级：两次文件只读对拍（复用首版冻结实现）在**业务重放 / 冲突判定之前**执行；
批内字节 / 摘要漂移一律 ``planning_source_drift`` 并回滚，不把漂移改判成版本更新。

错误码口径：``revision_invalid`` 保留原 code，其余沿用原 code，不弱化、不改全局枚举；
本模块自定义 wrapper ``PlanningSnapshotRevisionSaveError`` 兼容首版服务与 revision
纯函数的**全部原码**。

依赖加载：只做**直接导入**（``contracts.planning_revision`` /
``contracts.planning_structure`` / ``contracts.planning_digest`` /
``service.planning_snapshot_import``）。本候选不搜索父目录、不读环境变量覆盖、不退化
成本地实现；调用方 / 测试负责把冻结的 formal 包根 ``shared/agent-orchestrator``
放到 ``sys.path``。

状态：实现候选；Worker 无 bash 权限，测试全部 **NOT_RUN**，由 cox 独立运行并判定。
完整 MVP / A 阶段**未完成**。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone

from contracts.planning_digest import PlanningDigestError
from contracts.planning_revision import (
    DECISION_APPEND_CANDIDATE,
    DECISION_CREATE_FIRST,
    DECISION_REPLAYED,
    PLANNING_REVISION_ERROR_CODES,
    REASON_NEW_CONTENT_REASON_INVALID,
    PlanningRevisionError,
    decide_planning_revision,
)
from contracts.planning_structure import validate_planning_structure
from service import planning_snapshot_import as _first_import

__all__ = [
    "PlanningSnapshotRevisionSaveError",
    "save_planning_snapshot_revision",
]


# ---------------------------------------------------------------------------
# 错误口径：wrapper 兼容首版服务与 revision 纯函数的全部原码（不改全局枚举）
# ---------------------------------------------------------------------------

#: 合并首版服务错误码与 revision 诊断码的默认消息表（只读复用，不新造同义码）。
_ERROR_MESSAGES = dict(_first_import.PLANNING_SNAPSHOT_IMPORT_ERROR_CODES)
_ERROR_MESSAGES.update(PLANNING_REVISION_ERROR_CODES)

#: 本 wrapper 可承载的全部原 code：首版服务码 + 其透传码 + revision 诊断码。
_ALLOWED_ERROR_CODES = (
    frozenset(_first_import.PLANNING_SNAPSHOT_IMPORT_ERROR_CODES)
    | frozenset(_first_import._PASS_THROUGH_ERROR_CODES)
    | frozenset(PLANNING_REVISION_ERROR_CODES)
)

#: 复用首版服务冻结的「事务失败并已回滚、零残留」语义码；不新造同义码、不改全局枚举。
_ERROR_TRANSACTION_FAILED = "planning_snapshot_import_failed"

#: 追加但未提供 ``new_content_reason`` 时的审计原因：明确说明本次是新 documentVersion。
_APPEND_VERSION_REASON_TEMPLATE = "new document version %s"

#: decide 判定读取既有行时只允许的六个字段；自报编号 / 身份一律不进判定。
_HISTORY_FIELDS = (
    "snapshotId",
    "snapshotRevision",
    "canonicalDigest",
    "documentVersion",
    "sourceSha256",
    "sourceBytes",
)


class PlanningSnapshotRevisionSaveError(ValueError):
    """revision 追加保存失败：调用方必须 fail closed，不写乐观状态、不自动重试。

    ``code`` 兼容首版服务与 revision 纯函数的全部原 code；``details`` 保留稳定
    ``reason`` 与字段路径，不回显正文 / 标题 / 内容。
    """

    def __init__(self, code, message=None, details=None):
        if code not in _ALLOWED_ERROR_CODES:
            raise ValueError(
                "unknown planning snapshot revision save error code: %r" % (code,)
            )
        self.code = code
        self.details = dict(details) if details else {}
        super(PlanningSnapshotRevisionSaveError, self).__init__(
            message or _ERROR_MESSAGES.get(code) or code
        )

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


def _wrap_import_error(exc):
    """沿用首版服务异常的原 code / 原 details，只换成统一 wrapper。"""
    details = dict(getattr(exc, "details", {}) or {})
    details.setdefault("cause", {"code": exc.code, "type": type(exc).__name__})
    return PlanningSnapshotRevisionSaveError(
        exc.code, message=str(exc), details=details
    )


def _wrap_revision_error(exc):
    """沿用 revision 纯函数异常的原 code / 原 reason；未知码兜底为 revision_invalid。"""
    raw_code = getattr(exc, "code", None)
    code = raw_code if raw_code in _ALLOWED_ERROR_CODES else "planning_revision_invalid"
    details = dict(getattr(exc, "details", {}) or {})
    details.setdefault("cause", {"code": raw_code, "type": type(exc).__name__})
    message = getattr(exc, "message", None) or str(exc)
    return PlanningSnapshotRevisionSaveError(code, message=message, details=details)


# ---------------------------------------------------------------------------
# 1) 受控入口关键字：``new_content_reason`` 只收 None 或非空非空白 str
# ---------------------------------------------------------------------------


def _validate_new_content_reason(new_content_reason):
    """只接受受控入口关键字 None 或非空非空白字符串；不收 payload 里的自报 reason。"""
    if new_content_reason is None:
        return
    if not isinstance(new_content_reason, str) or new_content_reason.strip() == "":
        raise PlanningSnapshotRevisionSaveError(
            "planning_revision_invalid",
            message="new_content_reason 必须是 None 或非空非空白字符串（只收受控入口关键字）",
            details={
                "reason": REASON_NEW_CONTENT_REASON_INVALID,
                "type": type(new_content_reason).__name__,
            },
        )


# ---------------------------------------------------------------------------
# 2) 事务内步骤：来源核验 -> 结构校验 -> 两次对拍 -> revision 判定 -> 插入
# ---------------------------------------------------------------------------


def _load_history(connection, project_id, planning_source_id):
    """在自己事务内读取本来源**全历史**（含重放所需的既有可信元数据）。

    只按 ``(project_id, planning_source_id)`` 精确取，不跨项目、不按标题 / 路径猜。
    """
    rows = connection.execute(
        "SELECT snapshot_id, snapshot_revision, canonical_digest, document_version, "
        "source_sha256, source_bytes, imported_at, imported_by_json, status "
        "FROM planning_snapshots WHERE project_id = ? AND planning_source_id = ? "
        "ORDER BY snapshot_revision",
        (project_id, planning_source_id),
    ).fetchall()
    history = []
    for row in rows:
        history.append(
            {
                "snapshotId": row[0],
                "snapshotRevision": row[1],
                "canonicalDigest": row[2],
                "documentVersion": row[3],
                "sourceSha256": row[4],
                "sourceBytes": row[5],
                "importedAt": row[6],
                "importedByJson": row[7],
                "status": row[8],
            }
        )
    return history


def _replayed_result(project_id, planning_source_id, history, decision,
                     canonical_digest, diagnostics):
    """重放：原样回显既有可信元数据，不创建编号 / 审计，也不写旧记录。"""
    match = None
    for row in history:
        if row["snapshotId"] == decision["snapshotId"]:
            match = row
            break
    if match is None:
        raise PlanningSnapshotRevisionSaveError(
            "planning_revision_invalid",
            message="重放判定指向的既有快照不在本来源历史内，拒绝",
            details={
                "reason": "replay_row_not_in_history",
                "snapshotId": decision["snapshotId"],
            },
        )
    try:
        imported_by = json.loads(match["importedByJson"])
    except (TypeError, ValueError) as exc:
        raise PlanningSnapshotRevisionSaveError(
            "planning_revision_invalid",
            message="既有快照的导入身份不是可读的受控元数据，拒绝重放",
            details={
                "reason": "replay_identity_unreadable",
                "snapshotId": match["snapshotId"],
            },
        ) from exc

    return {
        "projectId": project_id,
        "planningSourceId": planning_source_id,
        "snapshotId": match["snapshotId"],
        "snapshotRevision": match["snapshotRevision"],
        "status": match["status"],
        "canonicalDigest": canonical_digest,
        "replayed": True,
        "diagnostics": list(diagnostics),
        "wouldActivate": False,
        "importedAt": match["importedAt"],
        "importedBy": imported_by,
    }


def _audit_reason(decision_kind, new_content_reason, document_version):
    """审计 reason 映射：首版沿用冻结 reason；追加优先原受控 reason，否则说明新版本。"""
    if decision_kind == DECISION_CREATE_FIRST:
        return _first_import.AUDIT_REASON
    if new_content_reason is not None:
        return new_content_reason
    return _APPEND_VERSION_REASON_TEMPLATE % document_version


def _insert_candidate(connection, snapshot, ctx, canonical_digest, diagnostics,
                      document_version, source_bytes, source_sha256,
                      snapshot_revision, audit_reason):
    """只 INSERT 一条快照 + 一条审计；返回本次候选保存结果（不含任何激活结论）。"""
    project_id = snapshot["projectId"]
    planning_source_id = snapshot["planningSourceId"]

    # 系统分配编号：UUID4 随机，不由路径 / 内容 / revision 派生。
    snapshot_id = _first_import.SNAPSHOT_ID_PREFIX + uuid.uuid4().hex
    audit_id = _first_import.AUDIT_ID_PREFIX + uuid.uuid4().hex
    imported_at = datetime.now(timezone.utc).isoformat()
    identity = ctx.to_dict()
    imported_by_json = _first_import._canonical_json(identity)

    stored_payload = dict(snapshot)
    stored_payload["snapshotId"] = snapshot_id
    stored_payload["snapshotRevision"] = snapshot_revision
    stored_payload["importedAt"] = imported_at
    stored_payload["importedBy"] = identity
    stored_payload["canonicalDigest"] = canonical_digest
    payload_json = _first_import._canonical_json(stored_payload)

    detail_json = _first_import._canonical_json(
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
            _first_import.SNAPSHOT_STATUS,
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
            _first_import.AUDIT_ACTION,
            imported_by_json,
            audit_reason,
            imported_at,
            detail_json,
        ),
    )

    return {
        "projectId": project_id,
        "planningSourceId": planning_source_id,
        "snapshotId": snapshot_id,
        "snapshotRevision": snapshot_revision,
        "status": _first_import.SNAPSHOT_STATUS,
        "canonicalDigest": canonical_digest,
        "replayed": False,
        "diagnostics": list(diagnostics),
        "wouldActivate": False,
        "importedAt": imported_at,
        "importedBy": identity,
    }


def _save_within_transaction(connection, snapshot, ctx, roots, new_content_reason):
    registered_project_id, source_kind, source_path, registered_root = (
        _first_import._load_registered_source(connection, snapshot)
    )
    common_root = _first_import._require_declaration_matches_registration(
        snapshot, source_kind, source_path, registered_root, roots
    )

    # 结构 / 摘要校验复用已通过冻结函数（独立深拷贝载荷 / 摘要 / 缺映射诊断）。
    try:
        validated = validate_planning_structure(
            snapshot,
            expected_project_id=registered_project_id,
            expected_planning_source_id=snapshot["planningSourceId"],
        )
    except PlanningDigestError as exc:
        raise _first_import._wrap_formal_error(exc) from exc

    # 两次受控只读预检 + 字节 / SHA 核验（复用首版冻结实现）。
    # 漂移在这里先于业务重放 / 冲突判定抛出，且不把漂移改判成版本更新。
    source_file = snapshot["sourceFile"]
    first = _first_import._read_source_twice(source_path, common_root, source_file)

    canonical_digest = validated["canonicalDigest"]
    diagnostics = validated["diagnostics"]
    document_version = source_file["documentVersion"]

    history = _load_history(
        connection, snapshot["projectId"], snapshot["planningSourceId"]
    )
    existing_for_decision = [
        {field: row[field] for field in _HISTORY_FIELDS} for row in history
    ]
    incoming = {
        "canonicalDigest": canonical_digest,
        "documentVersion": document_version,
        "sourceSha256": first.get("sha256"),
        "sourceBytes": first.get("bytes"),
    }
    decision = decide_planning_revision(
        existing_for_decision, incoming, new_content_reason=new_content_reason
    )

    kind = decision["decision"]
    if kind == DECISION_REPLAYED:
        return _replayed_result(
            snapshot["projectId"],
            snapshot["planningSourceId"],
            history,
            decision,
            canonical_digest,
            diagnostics,
        )
    if kind not in (DECISION_CREATE_FIRST, DECISION_APPEND_CANDIDATE):
        raise PlanningSnapshotRevisionSaveError(
            "planning_revision_invalid",
            message="规划的 revision 判定不是受控结论，拒绝保存",
            details={"reason": "unknown_decision", "decision": kind},
        )

    audit_reason = _audit_reason(kind, new_content_reason, document_version)
    return _insert_candidate(
        connection,
        validated["payload"],
        ctx,
        canonical_digest,
        diagnostics,
        document_version,
        first.get("bytes"),
        first.get("sha256"),
        decision["snapshotRevision"],
        audit_reason,
    )


# ---------------------------------------------------------------------------
# 3) 公开入口
# ---------------------------------------------------------------------------


def _save(connection, snapshot, context, allowed_roots, new_content_reason):
    # 顺序固定：受控授权 -> 入口关键字 -> 载荷基础校验 -> 受控允许根 -> 存储就绪 ->
    # 单个 BEGIN IMMEDIATE 自有事务（来源核验、结构校验、两次对拍、revision 判定、插入）。
    ctx = _first_import._authorize(context)
    _validate_new_content_reason(new_content_reason)
    payload = _first_import._validate_snapshot_payload(snapshot)
    roots = _first_import._validate_allowed_roots(allowed_roots)
    _first_import._require_storage(connection)

    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.Error as exc:
        # 未开启事务，因此这里不回滚；并发写锁 busy 明确失败、绝不自动重试。
        raise PlanningSnapshotRevisionSaveError(
            _ERROR_TRANSACTION_FAILED,
            message="无法开启快照 revision 保存事务（含并发写锁 busy），不自动重试：%s" % exc,
            details={"reason": "begin_failed", "error": type(exc).__name__},
        ) from exc

    try:
        result = _save_within_transaction(
            connection, payload, ctx, roots, new_content_reason
        )
        connection.execute("COMMIT")
    except PlanningSnapshotRevisionSaveError:
        # 本模块自己抛出的结构化拒绝：回滚自己开启的事务后原样抛出。
        _first_import._rollback(connection)
        raise
    except (_first_import.PlanningSnapshotImportError, PlanningRevisionError):
        # 首版服务 / revision 纯函数的结构化拒绝：回滚自己开启的事务（含漂移 / 冲突）。
        _first_import._rollback(connection)
        raise
    except sqlite3.Error as exc:
        _first_import._rollback(connection)
        raise PlanningSnapshotRevisionSaveError(
            _ERROR_TRANSACTION_FAILED,
            message="快照 revision 保存事务失败并已回滚自己开启的事务：%s" % exc,
            details={"reason": "transaction_failed", "error": type(exc).__name__},
        ) from exc
    except Exception as exc:  # noqa: BLE001 - 任何意外失败同样要求零残留
        _first_import._rollback(connection)
        raise PlanningSnapshotRevisionSaveError(
            _ERROR_TRANSACTION_FAILED,
            message="快照 revision 保存出现意外失败并已回滚：%s" % exc,
            details={"reason": "unexpected_failure", "error": type(exc).__name__},
        ) from exc
    return result


def save_planning_snapshot_revision(connection, snapshot, context=None,
                                    allowed_roots=None, *, new_content_reason=None):
    """把一次受控结构化规划载荷按**候选 revision** 语义保存（复用首版冻结校验）。

    受控授权 / 载荷 / 允许根 / 存储就绪复用首版服务的冻结 helper；revision 判定复用
    ``decide_planning_revision``。只 INSERT 快照 + 审计各一条（同一 ``BEGIN IMMEDIATE``
    事务原子提交），``status = candidate``、``wouldActivate = false``。

    ``new_content_reason`` 只接受受控入口关键字 ``None`` 或非空非空白字符串；不接受
    payload 中自报的编号 / 身份 / digest / revision / reason。

    返回值只描述「本次候选保存事实」：``projectId`` / ``planningSourceId`` /
    ``snapshotId`` / ``snapshotRevision`` / ``status`` / ``canonicalDigest`` /
    ``replayed`` / ``diagnostics`` / ``wouldActivate(false)`` / ``importedAt`` /
    ``importedBy``。**不返回激活 / 验收 / 可派发等任何结论。**

    失败一律抛 :class:`PlanningSnapshotRevisionSaveError`（兼容全部原 code），
    调用方必须 fail closed，不自动重试。
    """
    try:
        return _save(connection, snapshot, context, allowed_roots, new_content_reason)
    except PlanningSnapshotRevisionSaveError:
        raise
    except _first_import.PlanningSnapshotImportError as exc:
        raise _wrap_import_error(exc) from exc
    except PlanningRevisionError as exc:
        raise _wrap_revision_error(exc) from exc
