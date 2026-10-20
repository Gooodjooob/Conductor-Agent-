"""D4-MVP20-013：规划快照来源的只读预检（候选实现）。

契约来源：
- ``docs/modules/agent-task-control-plane-d4-interface-contract.md`` §12.2
  （受控来源读取：不读取凭据 / 私有会话库 / 整目录；读取后复核字节与摘要漂移）
- 只读复用：``shared/agent-orchestrator/contracts/document_io.py`` 的
  ``read_source_file`` / ``bytes_digest`` / ``DocumentContractError``
- 同级参考实现（同口径状态词表、fail-closed 允许根校验）：
  ``shared/agent-orchestrator/service/project_intake.py``

本卡范围（严格限定）：
- 只对「规划快照声明的来源文件」做只读预检：受控允许根、普通文件、实际字节数、sha256。
- 结论词表：``read`` / ``pending`` / ``out_of_scope`` / ``rejected`` / ``unreadable`` /
  ``drift``；相对路径、``..``、符号链接、凭据 / 私有目录、不可读文件、大小超限
  各自给出确定结论与稳定 reason 码。
- **复用** document_io 的路径安全与读取规则（含允许根、符号链接、普通文件、
  大小上限、UTF-8 解码），本模块不复制、不弱化这些规则。
- 明确不做：不建库、不写来源文件、不导入快照、不生成 ``snapshotId`` /
  ``snapshotRevision``、不激活规划、不生成 task / attempt / delivery / review 事实、
  不调宿主。``preflight_sources`` 显式返回 ``wouldWrite`` / ``wouldImport`` /
  ``wouldActivate`` 恒为 ``False``，并在 ``sideEffects`` 逐项声明未发生。
- ``expected_bytes`` / ``expected_sha256`` 由受控上层（已登记来源引用）提供；
  本模块只做**精确比对**，不查登记库、不核实来源归属，也不授予任何身份权限。
"""

from __future__ import annotations

import json
import os
import re
import sys

__all__ = [
    "DENY_PATH_MARKERS",
    "DEFAULT_MAX_BYTES",
    "PLANNING_PREFLIGHT_ERROR_CODES",
    "PLANNING_SOURCE_STATUSES",
    "PlanningPreflightError",
    "bytes_digest",
    "compare_reads",
    "preflight_source",
    "preflight_sources",
    "read_source_file",
    "snapshot_sources",
    "sources_digest",
]

# ---------------------------------------------------------------------------
# 复用受控只读契约（不复制实现、不弱化规则；加载失败一律 fail closed）
# ---------------------------------------------------------------------------

_CONTRACTS_ROOT_ENV = "D4_CONTRACTS_ROOT"
_CONTRACTS_RELATIVE = os.path.join("shared", "agent-orchestrator")
_DOCUMENT_IO_RELATIVE = os.path.join("contracts", "document_io.py")


def _iter_candidate_contracts_roots():
    """候选只读契约根：先环境变量，再从本文件向上寻找受控契约目录。"""
    override = os.environ.get(_CONTRACTS_ROOT_ENV)
    if isinstance(override, str) and override.strip():
        yield override
    current = os.path.dirname(os.path.abspath(__file__))
    for _ in range(12):
        yield os.path.join(current, _CONTRACTS_RELATIVE)
        parent = os.path.dirname(current)
        if parent == current:
            return
        current = parent


def _load_document_io():
    """加载 ``contracts.document_io``；只读导入，无写入、无网络、无依赖安装。"""
    try:
        from contracts import document_io as module
        return module
    except ImportError:
        pass
    for root in _iter_candidate_contracts_roots():
        if not isinstance(root, str) or not root.strip():
            continue
        if not os.path.isfile(os.path.join(root, _DOCUMENT_IO_RELATIVE)):
            continue
        if root not in sys.path:
            sys.path.append(root)
        try:
            from contracts import document_io as module
            return module
        except ImportError:
            continue
    raise ImportError(
        "无法加载受控读取契约 contracts.document_io；本候选拒绝退化成本地读取实现"
    )


_document_io = _load_document_io()

# 直接复用受控读取函数与稳定摘要函数（测试可断言是同一对象，证明是复用而非重写）。
read_source_file = _document_io.read_source_file
bytes_digest = _document_io.bytes_digest
DocumentContractError = _document_io.DocumentContractError

# 读取上限与 document_io 同口径；实际调用时仍由服务受控配置注入，不从输入取值。
DEFAULT_MAX_BYTES = getattr(_document_io, "MAX_FILE_BYTES", 1024 * 1024)

# ---------------------------------------------------------------------------
# 稳定错误码 / 状态词表（本卡冻结）
# ---------------------------------------------------------------------------

PLANNING_PREFLIGHT_ERROR_CODES = {
    "planning_preflight_not_configured": "预检缺少受控允许根，默认拒绝（不得由输入自行指定）",
    "planning_source_preflight_invalid": "预检入参不符合受控形态，拒绝继续",
}

# 每份来源的确定结论（与 service/project_intake.py 同口径，另行区分 read / rejected）。
PLANNING_SOURCE_STATUSES = (
    "read",     # 已受控读取并登记实际 path / bytes / sha256
    "pending",      # 仍是模板占位符，必须标「待补充」，不能算可应用
    "out_of_scope",  # 越出服务受控允许根
    "rejected",       # 相对路径、`..` 逃逸、符号链接或凭据 / 私有目录，明确拒绝
    "unreadable",   # 不存在 / 不是普通文件 / 不可读 / 非 UTF-8 / 超限
    "drift",        # 读取期间字节漂移，或与已登记期望摘要不一致，本批停止
)

# 明确不读的来源：凭据、私钥、私有会话库与系统目录。
# 依接口 §12.2「不读取凭据、私有会话库或整目录」；命中即拒绝该项来源，
# 不是结构错误，也不算「来源可应用」。
DENY_PATH_MARKERS = (
    "/.ssh/",
    "/.aws/",
    "/.gnupg/",
    "/.kube/",
    "/.docker/",
    "/.netrc",
    "/.npmrc",
    "/credentials",
    "/secret",
    "/id_rsa",
    "/id_ed25519",
    "/.env",
    "/.qclaw/",
    "/.hermes/sessions",
    "/Library/Keychains/",
)

# 模板占位符：整值形态（不猜测路径内部片段）。
_PLACEHOLDER_ANGLE = re.compile(r"<[^<>]*>")
_PLACEHOLDER_BRACES = ("{{", "}}")
_PLACEHOLDER_WORDS = ("TODO", "TBD", "TBC", "待补充")

# document_io 错误码 -> 本卡状态（未列出的错误码落 unreadable，保持 fail closed）。
_STATUS_BY_DOCUMENT_ERROR = {
    "path_escape": "out_of_scope",
    "path_symlink_rejected": "rejected",
    "path_not_absolute": "rejected",
}


class PlanningPreflightError(ValueError):
    """预检入参 / 服务配置错误：调用方必须 fail closed，不产生任何来源结论。"""

    def __init__(self, code, message=None, details=None):
        if code not in PLANNING_PREFLIGHT_ERROR_CODES:
            raise ValueError("unknown planning preflight error code: %r" % (code,))
        self.code = code
        self.details = dict(details) if details else {}
        super(PlanningPreflightError, self).__init__(
            message or PLANNING_PREFLIGHT_ERROR_CODES[code]
        )

    def to_dict(self):
        return {"code": self.code, "message": str(self), "details": self.details}


# ---------------------------------------------------------------------------
# 受控配置与输入形态
# ---------------------------------------------------------------------------


def _assert_allowed_roots(allowed_roots):
    """允许根必须由服务配置注入；缺配置一律 fail closed，不从输入取上限。"""
    if not isinstance(allowed_roots, (list, tuple)) or not allowed_roots:
        raise PlanningPreflightError(
            "planning_preflight_not_configured",
            message="预检缺少受控允许根，默认拒绝（不得由输入自行指定）",
            details={"allowedRoots": allowed_roots},
        )
    roots = []
    for entry in allowed_roots:
        if not isinstance(entry, str) or not entry.strip() or not entry.startswith("/"):
            raise PlanningPreflightError(
                "planning_preflight_not_configured",
                message="允许根必须是非空绝对路径字符串",
                details={"entry": entry},
            )
        roots.append(os.path.realpath(entry))
    return roots


def _is_placeholder(value):
    """整值占位符判定（如 ``<planning-source>`` / ``{{source}}`` / ``TODO``）。"""
    text = value.strip()
    if not text:
        return False
    if _PLACEHOLDER_ANGLE.fullmatch(text):
        return True
    if all(marker in text for marker in _PLACEHOLDER_BRACES):
        return True
    return text.upper() in _PLACEHOLDER_WORDS or text in _PLACEHOLDER_WORDS


def _is_denied(path):
    lowered = path.lower()
    return any(marker.lower() in lowered for marker in DENY_PATH_MARKERS)


def _readback(path, max_bytes):
    """读取后复核：重新只读同一文件字节，用于本次预检内的漂移检测。

    只读取 ``max_bytes + 1`` 字节上限，避免被超大文件拖垮；不写任何文件。
    """
    with open(path, "rb") as handle:
        return handle.read(max_bytes + 1)


def _new_result(item):
    result = {
        "pointer": None,
        "role": None,
        "claimedPath": None,
        "status": "unreadable",
        "reason": None,
        "absolutePath": None,
        "bytes": None,
        "sha256": None,
        "stable": None,
        "regularFile": None,
        "drift": None,
    }
    if isinstance(item, dict):
        pointer = item.get("pointer")
        role = item.get("role")
        claimed = item.get("path")
        result["pointer"] = pointer if isinstance(pointer, str) else None
        result["role"] = role if isinstance(role, str) else None
        result["claimedPath"] = claimed if isinstance(claimed, str) else None
    return result


def _drift_result(result, reason, drift):
    result.update({
        "status": "drift",
        "reason": reason,
        "stable": False,
        "drift": drift,
    })
    return result


# ---------------------------------------------------------------------------
# 单份来源只读预检
# ---------------------------------------------------------------------------


def preflight_source(item, allowed_roots, max_bytes=DEFAULT_MAX_BYTES,
                     expected_bytes=None, expected_sha256=None):
    """单份规划来源的只读预检；任何失败都转成确定状态，不抛业务异常、不猜结论。

    - ``allowed_roots`` / ``max_bytes`` 由服务受控配置注入；
    - ``expected_bytes`` / ``expected_sha256`` 为已登记来源的实际值，只做精确比对；
    - 返回 dict 只描述来源事实，不产生任何编号或执行事实。
    """
    if not isinstance(item, dict):
        raise PlanningPreflightError(
            "planning_source_preflight_invalid",
            message="来源项必须是对象",
            details={"item": type(item).__name__},
        )
    if not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes <= 0:
        raise PlanningPreflightError(
            "planning_source_preflight_invalid",
            message="max_bytes 必须是正整数",
            details={"maxBytes": max_bytes},
        )
    roots = _assert_allowed_roots(allowed_roots)

    result = _new_result(item)
    claimed = result["claimedPath"]

    # 1) 缺路径 / 非法形态：确定结论，不进入读取。
    if not isinstance(claimed, str) or not claimed.strip():
        result["status"] = "unreadable"
        result["reason"] = "source_path_invalid"
        return result

    # 2) 模板占位符：结构层放行，但绝不能算「可应用」。
    if _is_placeholder(claimed):
        result["status"] = "pending"
        result["reason"] = "template_placeholder"
        return result

    # 3) 相对路径 / `..` 逃逸：先给出独立 reason，避免与允许根越界混淆。
    if not claimed.startswith("/"):
        result["status"] = "rejected"
        result["reason"] = "path_not_absolute"
        return result
    if ".." in claimed.split("/"):
        result["status"] = "rejected"
        result["reason"] = "path_dotdot_rejected"
        return result

    # 4) 凭据 / 私有目录 / 私有会话库：命中即拒绝（先于读取，也不进允许根判定）。
    if _is_denied(claimed):
        result["status"] = "rejected"
        result["reason"] = "credential_or_private_source_denied"
        return result

    # 5) 受控读取：复用 document_io 的允许根 / 符号链接 / 普通文件 / 大小 / 解码规则。
    try:
        meta, _text = read_source_file(claimed, allowed_roots=roots, max_bytes=max_bytes)
    except DocumentContractError as exc:
        result["status"] = _STATUS_BY_DOCUMENT_ERROR.get(exc.code, "unreadable")
        result["reason"] = exc.code
        if exc.code == "document_too_large":
            result["bytes"] = exc.details.get("bytes")
        result["absolutePath"] = os.path.realpath(claimed) if os.path.exists(claimed) else None
        return result

    result["absolutePath"] = meta["path"]
    result["bytes"] = meta["bytes"]
    result["sha256"] = meta["sha256"]
    result["regularFile"] = True

    # 6) 读取后复核：同一文件二次只读比对，字节或摘要变化即视为来源漂移。
    try:
        raw_after = _readback(meta["path"], max_bytes)
    except OSError as exc:
        result = _drift_result(result, "planning_source_drift", {
            "beforeBytes": meta["bytes"],
            "beforeSha256": meta["sha256"],
            "readbackError": type(exc).__name__,
        })
        return result

    after_bytes = len(raw_after)
    after_digest = bytes_digest(raw_after)
    if after_bytes != meta["bytes"] or after_digest != meta["sha256"]:
        result = _drift_result(result, "planning_source_drift", {
            "beforeBytes": meta["bytes"],
            "beforeSha256": meta["sha256"],
            "afterBytes": after_bytes,
            "afterSha256": after_digest,
        })
        return result

    # 7) 与已登记期望值精确比对（不做模糊匹配、不查登记库）。
    checked = {}
    if isinstance(expected_bytes, int) and not isinstance(expected_bytes, bool):
        checked["bytes"] = expected_bytes
    if isinstance(expected_sha256, str) and expected_sha256:
        checked["sha256"] = expected_sha256
    mismatch = [field for field, value in sorted(checked.items()) if value != result[field]]
    if mismatch:
        drift = {
            "expected": checked,
            "actual": {"bytes": result["bytes"], "sha256": result["sha256"]},
            "mismatch": mismatch,
        }
        return _drift_result(result, "planning_source_mismatch", drift)

    result["status"] = "read"
    result["reason"] = None
    result["stable"] = True
    return result


# ---------------------------------------------------------------------------
# 批次汇总（只读，不落盘）
# ---------------------------------------------------------------------------


def snapshot_sources(items):
    """把预检结果压成可对拍的来源快照（只登记来源事实，不含正文）。"""
    return [
        {
            "pointer": item.get("pointer"),
            "role": item.get("role"),
            "path": item.get("absolutePath") or item.get("claimedPath"),
            "bytes": item.get("bytes"),
            "sha256": item.get("sha256"),
            "status": item.get("status"),
        }
        for item in items
    ]


def sources_digest(items):
    """来源清单的确定性摘要：同一批来源必须得到同一结果（供前后对拍）。"""
    encoded = json.dumps(
        snapshot_sources(items), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return bytes_digest(encoded)


def _source_key(item):
    path = item.get("absolutePath") or item.get("claimedPath") or item.get("path")
    return {"path": path, "bytes": item.get("bytes"), "sha256": item.get("sha256")}


def compare_reads(before, after):
    """比对同一来源两次只读结果（只读，不写库、不落盘、不改输入）。

    判定口径：path / bytes / sha256 任一不同即漂移。
    """
    before_key = _source_key(before)
    after_key = _source_key(after)
    changed = [field for field in ("path", "bytes", "sha256")
               if before_key[field] != after_key[field]]
    return {
        "same": not changed,
        "changedFields": changed,
        "before": before_key,
        "after": after_key,
    }


def preflight_sources(sources, allowed_roots, max_bytes=DEFAULT_MAX_BYTES):
    """批只读预检入口：逐份预检 + 汇总，不建库、不导入、不激活、不派发。

    返回值中 ``wouldWrite`` / ``wouldImport`` / ``wouldActivate`` 恒为 ``False``，
    且不生成 snapshotId / revision 与任何 task / attempt / delivery / review 事实。
    """
    roots = _assert_allowed_roots(allowed_roots)
    if not isinstance(sources, (list, tuple)):
        raise PlanningPreflightError(
            "planning_source_preflight_invalid",
            message="sources 必须是数组",
            details={"type": type(sources).__name__},
        )

    items = [preflight_source(item, roots, max_bytes) for item in sources]

    blocked = [item for item in items if item["status"] != "read"]
    drifted = [item for item in items if item["status"] == "drift"]
    pending = [item for item in items if item["status"] == "pending"]

    not_applicable = []
    if not items:
        not_applicable.append("no_declared_sources")
    if drifted:
        not_applicable.append("sources_drifted")
    if any(item["status"] == "out_of_scope" for item in items):
        not_applicable.append("sources_out_of_scope")
    if any(item["status"] == "rejected" for item in items):
        not_applicable.append("sources_denied")
    if any(item["status"] == "unreadable" for item in items):
        not_applicable.append("sources_unreadable")
    if pending:
        not_applicable.append("template_placeholders_present")

    return {
        "planningSourcePreflight": {
            "checked": True,
            "allowedRoots": roots,
            "maxBytes": max_bytes,
            "items": items,
            "snapshot": snapshot_sources(items),
            "digest": sources_digest(items),
            "allReadable": not blocked,
            "driftDetected": bool(drifted),
        },
        "counts": {
            "declared": len(items),
            "read": len(items) - len(blocked),
            "blocked": len(blocked),
            "pending": len(pending),
            "drifted": len(drifted),
        },
        "applicable": not not_applicable,
        "notApplicableReasons": not_applicable,
        "wouldWrite": False,
        "wouldImport": False,
        "wouldActivate": False,
        "sideEffects": {
            "databaseOpened": False,
            "databaseCreated": False,
            "sourceFileWritten": False,
            "snapshotImported": False,
            "snapshotIdGenerated": False,
            "revisionGenerated": False,
            "planningActivated": False,
            "taskCreated": False,
            "attemptCreated": False,
            "deliveryWritten": False,
            "reviewWritten": False,
            "hostCalled": False,
        },
        # 显式声明本次预检未生成任何受控编号或执行事实（空数组，不是省略字段）。
        "generatedIdentifiers": [],
        "executionFacts": [],
    }
