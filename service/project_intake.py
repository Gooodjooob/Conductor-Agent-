"""D4-PROJECT-01：项目接入资料的无副作用预检（inspect）。

只做三件事：

1. **结构校验**：调用 ``contracts/project_intake`` 的纯校验；结论**不依赖**
   被引用的来源文件是否存在，缺文件不得让结构结论变成「不合法」；
2. **来源预检**：只在受控允许根内安全读取权威来源，登记实际路径、大小与摘要；
   占位符、越界、凭据目录、不可读都单独标明，不与结构错误混算；
3. **汇总**：给出拟写入清单、错误与待补项，并显式声明
   ``wouldWrite=False`` / ``wouldDispatch=False``。

明确不做：不建库、不写任何文件、不生成快照、不调 Adapter / 宿主、不提交真实
Agent、不做 Git / 同步 / 发布。合法预检结果自带每份来源的摘要，供下一卡比对
来源漂移（同 ``intakeId+intakeRevision`` 重跑必须得到同一份来源清单）。
"""

from __future__ import annotations

import os

from contracts.document_io import (
    DocumentContractError,
    bytes_digest,
    read_source_file,
)
from contracts.project_intake import (
    INTAKE_JSON_MAX_BYTES,
    MATERIAL_MAX_BYTES,
    ProjectIntakeContractError,
    collect_material_paths,
    collect_update_paths,
    detect_schema_version,
    find_placeholders,
    parse_intake_json,
    summarize_intake,
    validate_document,
)

# 允许的来源角色：获批的产品 / 卡 / 报告类文件
SOURCE_ROLES = ("source", "material", "card", "report")

# 明确不读的来源：凭据、私钥、私有会话库与系统目录
# 依接口 §12.2「不读取凭据、私有会话库或整目录」；命中即拒绝该项来源，
# 不是结构错误，也不算「来源可应用」。
_DENY_PATH_MARKERS = (
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

# 每份来源的预检结论词表
MATERIAL_STATUSES = (
    "read",        # 已安全读取并登记摘要
    "pending",     # 仍是模板占位符，必须标「待补充」，不能算可应用
    "unreadable",  # 不存在 / 不是普通文件 / 不可读 / 非 UTF-8 / 超限
    "out_of_scope",  # 越出服务受控允许根
    "rejected",    # 命中凭据/私有目录或符号链接，明确拒绝
    "drift",       # 读取期间字节/摘要发生变化，本批停止
)


def _is_placeholder(value):
    """与契约层同口径的占位符判定（避免两层判定不一致）。"""
    return bool(find_placeholders({"probe": value}))


def _is_denied(path):
    lowered = path.lower()
    return any(marker.lower() in lowered for marker in _DENY_PATH_MARKERS)


def _assert_allowed_roots(allowed_roots):
    """允许根必须由服务配置注入；缺配置一律 fail closed，不从 payload 取上限。"""
    if not allowed_roots:
        raise ProjectIntakeContractError(
            "intake_not_configured",
            message="预检缺少受控允许根，默认拒绝（不得由 payload 自行指定）",
            details={"allowedRoots": allowed_roots},
        )
    roots = []
    for entry in allowed_roots:
        if not isinstance(entry, str) or not entry.strip() or not entry.startswith("/"):
            raise ProjectIntakeContractError(
                "intake_not_configured",
                message="允许根必须是非空绝对路径字符串",
                details={"entry": entry},
            )
        roots.append(os.path.realpath(entry))
    return roots


def preflight_material(item, allowed_roots, material_max_bytes=MATERIAL_MAX_BYTES):
    """单份来源的只读预检；任何失败都转成确定状态，不抛异常、不猜结论。"""
    claimed = item.get("path")
    result = {
        "pointer": item.get("pointer"),
        "role": item.get("role"),
        "claimedPath": claimed,
        "status": "unreadable",
        "absolutePath": None,
        "bytes": None,
        "sha256": None,
        "reason": None,
    }

    if not isinstance(claimed, str) or not claimed.strip():
        result["reason"] = "intake_structure_invalid"
        return result

    if _is_placeholder(claimed):
        # 模板占位符：结构层放行，但绝不能算「可应用」（任务卡实施步骤 3）
        result["status"] = "pending"
        result["reason"] = "template_placeholder"
        return result

    if not claimed.startswith("/"):
        result["status"] = "rejected"
        result["reason"] = "path_not_absolute"
        return result

    if _is_denied(claimed):
        result["status"] = "rejected"
        result["reason"] = "credential_or_private_source_denied"
        return result

    try:
        meta, _text = read_source_file(
            claimed, allowed_roots=allowed_roots, max_bytes=material_max_bytes
        )
    except DocumentContractError as exc:
        if exc.code == "path_escape":
            result["status"] = "out_of_scope"
        elif exc.code == "path_symlink_rejected":
            result["status"] = "rejected"
        elif exc.code == "document_too_large":
            result["status"] = "unreadable"
            result["bytes"] = exc.details.get("bytes")
        else:
            result["status"] = "unreadable"
        result["reason"] = exc.code
        result["absolutePath"] = os.path.realpath(claimed) if os.path.exists(claimed) else None
        return result

    # 读取后复核：字节数变化即视为来源漂移，本批停止（接口 §12.2）
    try:
        after_size = os.path.getsize(meta["path"])
    except OSError:
        after_size = None
    if after_size is not None and after_size != meta["bytes"]:
        result.update({
            "status": "drift",
            "reason": "intake_source_drift",
            "absolutePath": meta["path"],
            "bytes": meta["bytes"],
            "sha256": meta["sha256"],
        })
        return result

    result.update({
        "status": "read",
        "absolutePath": meta["path"],
        "bytes": meta["bytes"],
        "sha256": meta["sha256"],
    })
    return result


def snapshot_materials(materials):
    """把预检结果压成可对拍的来源快照（仅登记来源事实，不含正文）。"""
    return [
        {
            "pointer": item.get("pointer"),
            "role": item.get("role"),
            "path": item.get("absolutePath") or item.get("claimedPath"),
            "bytes": item.get("bytes"),
            "sha256": item.get("sha256"),
            "status": item.get("status"),
        }
        for item in materials
    ]


def compare_material_snapshots(before, after):
    """比对两次来源快照，返回漂移明细；供下一卡判断来源是否变化。

    判定口径：pointer 相同而 path/bytes/sha256 任一不同即漂移；新增/缺失也列出。
    """
    indexed_before = {item.get("pointer"): item for item in before}
    indexed_after = {item.get("pointer"): item for item in after}
    drift = []
    for pointer in sorted(set(indexed_before) | set(indexed_after)):
        old = indexed_before.get(pointer)
        new = indexed_after.get(pointer)
        if old is None:
            drift.append({"pointer": pointer, "change": "added", "after": new})
            continue
        if new is None:
            drift.append({"pointer": pointer, "change": "removed", "before": old})
            continue
        if (old.get("path"), old.get("bytes"), old.get("sha256")) != (
            new.get("path"), new.get("bytes"), new.get("sha256")
        ):
            drift.append({"pointer": pointer, "change": "changed",
                          "before": old, "after": new})
    return drift


def _write_preview(doc_type, obj, materials, placeholders):
    """拟写入清单：本卡只做预检，所以任何情况下都不落盘。"""
    counts = summarize_intake(obj) if doc_type == "intake" else {}
    plan = []
    if doc_type == "intake":
        plan.append({
            "target": "project-registry",
            "action": "register-projects",
            "items": counts.get("projectIds", []),
            "note": "登记片（导入卡实现）；本卡未实现，不落盘",
        })
    destinations = sorted({
        item.get("absolutePath") or item.get("claimedPath")
        for item in materials
        if item.get("status") in ("read", "pending")
    })
    return {
        "wouldWrite": False,
        "wouldDispatch": False,
        "plan": plan,
        "plannedWriteTargets": destinations,
        "blockedBy": [item["pointer"] for item in materials
                      if item["status"] != "read"][:50],
        "pendingReplacements": [item["path"] for item in placeholders][:50],
    }


def preflight_document(path, doc_type=None, allowed_roots=None,
                       material_max_bytes=MATERIAL_MAX_BYTES,
                       json_max_bytes=INTAKE_JSON_MAX_BYTES):
    """只读预检入口：解析 + 结构校验 + 来源预检 + 拟写入清单。

    - ``doc_type`` 缺省时按 ``schemaVersion`` 判断；仍无法判断则明确拒绝；
    - ``allowed_roots`` 与两个上限均由服务受控配置注入，**不读 payload 里的任何上限**；
    - 全程不建库、不写盘、不调宿主。
    """
    _assert_allowed_roots(allowed_roots)
    if doc_type is not None and doc_type not in ("intake", "update"):
        raise ProjectIntakeContractError(
            "intake_structure_invalid",
            message="doc_type 只接受 intake / update",
            details={"docType": doc_type},
        )

    meta, text = read_source_file(path, allowed_roots=allowed_roots, max_bytes=json_max_bytes)
    obj = parse_intake_json(text)

    resolved_type = doc_type or detect_schema_version(obj)
    if resolved_type is None:
        raise ProjectIntakeContractError(
            "intake_structure_invalid",
            message="无法从 schemaVersion 判断文件类型，请显式指定 doc_type",
            details={"schemaVersion": obj.get("schemaVersion") if isinstance(obj, dict) else None},
        )

    errors = validate_document(obj, resolved_type)
    placeholders = find_placeholders(obj)

    declared = collect_material_paths(obj) if resolved_type == "intake" else collect_update_paths(obj)
    materials = [preflight_material(item, allowed_roots, material_max_bytes)
                 for item in declared]

    blocked = [item for item in materials if item["status"] != "read"]
    not_applicable = []
    if errors:
        not_applicable.append("structure_invalid")
    if placeholders:
        not_applicable.append("template_placeholders_present")
    if blocked:
        not_applicable.append("sources_not_all_readable")

    return {
        "documentType": resolved_type,
        "schemaVersion": obj.get("schemaVersion") if isinstance(obj, dict) else None,
        "sourceFile": {
            "path": meta["path"],
            "bytes": meta["bytes"],
            "sha256": meta["sha256"],
        },
        "structure": {
            "ok": not errors,
            "errorCount": len(errors),
            "errors": errors,
        },
        # 结构结论与来源结论分开陈述：缺来源文件不等于结构不合法
        "sourcePreflight": {
            "checked": True,
            "allowedRoots": [os.path.realpath(root) for root in allowed_roots],
            "materialMaxBytes": material_max_bytes,
            "items": materials,
            "snapshot": snapshot_materials(materials),
            "allReadable": not blocked,
        },
        "placeholders": placeholders,
        "summary": summarize_intake(obj) if resolved_type == "intake" else {
            "projectId": obj.get("projectId"),
            "taskId": obj.get("taskId"),
            "updateId": obj.get("updateId"),
            "kind": obj.get("kind"),
        },
        "applicable": not not_applicable,
        "notApplicableReasons": not_applicable,
        "writePreview": _write_preview(resolved_type, obj, materials, placeholders),
        "sideEffects": {
            "databaseOpened": False,
            "databaseCreated": False,
            "snapshotWritten": False,
            "hostCalled": False,
            "fileWritten": False,
        },
    }


def material_digest(materials):
    """来源清单的确定性摘要：同一批来源必须得到同一结果（供前后对拍）。"""
    return bytes_digest(
        repr(snapshot_materials(materials)).encode("utf-8")
    )


__all__ = [
    "SOURCE_ROLES",
    "MATERIAL_STATUSES",
    "preflight_material",
    "preflight_document",
    "snapshot_materials",
    "compare_material_snapshots",
    "material_digest",
]
