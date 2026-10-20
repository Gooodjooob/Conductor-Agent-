"""受控路径校验：把 writeScope / 交付文件限制在 workspace 上限内。

规则（对应 R1 返工卡“必须修正 #4” 与 R2 返工卡“必须修正 #2”）：
- workspace 上限由**服务配置注入**的 allowed roots 决定；调用者不能从任务 payload 自定上限。
- workspace 必须是绝对路径，且规范化（realpath）后必须落在某个 allowed root 内；
  未配置或非法配置的 allowed roots 一律 fail closed。
- writeScope 是显式路径数组；每个条目（相对或绝对）规范化后必须落在 workspace 内。
- 交付 changedFiles / reportPath 规范化后必须落在 workspace；
  changedFiles 还必须落在某个 writeScope 条目内。
- 拒绝 `..` 逃逸、绝对路径逃逸和符号链接逃逸（统一用 realpath 解析）。

注意：这里只是**服务声明校验**（声明层），不等于宿主文件系统的实际写入隔离；
真实隔离须由接入层/沙箱另行保证（见能力报告说明）。
"""
from __future__ import annotations

import os

from contracts.schemas import ContractError


def _abs(path):
    return os.path.realpath(os.path.abspath(path))


def resolve_allowed_roots(allowed_roots):
    """规范化服务配置注入的 allowed roots；缺失/非法配置一律 fail closed。

    这些根是服务侧受控配置，**不来自任务 payload**。缺省为空时拒绝一切 workspace，
    避免“默认放开”。
    """
    if not allowed_roots:
        raise ContractError(
            "permission_denied",
            message="服务未配置受控允许根（allowed roots），默认拒绝",
            details={"allowedRoots": allowed_roots},
        )
    roots = []
    for entry in allowed_roots:
        if not isinstance(entry, str) or not entry.strip() or not entry.startswith("/"):
            raise ContractError(
                "permission_denied",
                message="允许根必须是非空绝对路径字符串",
                details={"entry": entry},
            )
        roots.append(_abs(entry))
    return roots


def resolve_workspace(workspace, allowed_roots=None):
    """规范化为受控 root；workspace 必须是绝对路径且落在服务配置的 allowed roots 内。"""
    if not isinstance(workspace, str) or not workspace.strip():
        raise ContractError(
            "invalid_task", message="workspace 必填", details={"workspace": workspace}
        )
    if not workspace.startswith("/"):
        raise ContractError(
            "invalid_task",
            message="workspace 必须是绝对路径",
            details={"workspace": workspace},
        )
    workspace_abs = _abs(workspace)
    roots = resolve_allowed_roots(allowed_roots)
    if not any(_within(root, workspace_abs) for root in roots):
        raise ContractError(
            "path_escape",
            message="workspace 越出服务允许根，拒绝（不能由调用者自行扩大目录上限）",
            details={"workspace": workspace_abs, "allowedRoots": roots},
        )
    return workspace_abs


def _within(root_abs, candidate):
    return candidate == root_abs or candidate.startswith(root_abs + os.sep)


def resolve_within(root, entry):
    """把 entry 解析为绝对路径，并要求落在 root 内；否则 path_escape。"""
    root_abs = _abs(root)
    if not isinstance(entry, str) or not entry.strip():
        raise ContractError(
            "path_escape", message="路径必须是非空字符串", details={"entry": entry}
        )
    candidate = entry if os.path.isabs(entry) else os.path.join(root_abs, entry)
    candidate = _abs(candidate)
    if not _within(root_abs, candidate):
        raise ContractError(
            "path_escape",
            message="路径越出受控 root",
            details={"root": root_abs, "candidate": candidate, "entry": entry},
        )
    return candidate


def resolve_scopes(workspace, write_scope):
    """把 writeScope 数组解析为受控 root 内的绝对路径列表。"""
    return [resolve_within(workspace, entry) for entry in write_scope]


def assert_within_scope(workspace, write_scope, entry):
    """要求 entry 落在某个 writeScope 条目内（用于交付文件）。"""
    target = resolve_within(workspace, entry)
    for scope_abs in resolve_scopes(workspace, write_scope):
        if _within(scope_abs, target):
            return target
    raise ContractError(
        "path_escape",
        message="文件不在授权的 writeScope 内",
        details={"workspace": _abs(workspace), "entry": entry},
    )
