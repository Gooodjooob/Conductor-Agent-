"""D4 §14.2：DSH 开发 Adapter 离线接线工厂（任务卡 D4-DSH-DEVELOPMENT-ADAPTER-OFFLINE-001）。

本模块只做**离线装配**：把「成员预检 → 环境 / 权限上限核对 → 开发上下文装配 → DSH 启动
规格 → 构造 legacy transport + 复用 ``DshSdkAdapter``」按固定顺序接起来，返回一个
**未启动、未投递**的 :class:`DshDevelopmentAdapter`。

它**不做**：不启动 DSH / 子进程、不 ``session/prompt``、不建 D4 任务 / attempt / review、
不写业务数据、不联网、不读凭据、不安装依赖，也不改变生产授权门（``scripts/d4ctl.py``
的 ``_dsh_sdk_authorized`` 仍恒为 ``False``；本模块刻意不 import 它，也不提供任何
绕过入口）。

固定顺序（顺序本身是安全设计）：

1. ``preflight_member``（复用 ``service.dsh_member_preflight``；``deny`` / ``recheck`` /
   身份非法 → **零 transport 构造**，不接受 payload 自报身份，也不接受调用方自带的
   “已通过预检”对象）；
2. 成员 / 环境 / 模型 / binding 与任务的一致性核对（``hostType=dsh``、
   ``adapterType=dsh-sdk``、``executionProfileRef=sdk-minimal``、
   ``hostAgentId=D4_DSH_SDK_AGENT_ID``、``fixed`` provider/model 非空、project/agent 与预检一致）；
3. 目录与权限上限核对：成员与环境的 ``allowedRoots`` 必须覆盖 workspace 与任务写范围；
   成员 / 环境 / 项目三层 ceiling 必须覆盖任务实际权限；四个高风险开关显式 ``false``；
   拒相对根、``..``、用户符号链接段；
4. ``build_development_context``（三份来源 SHA 由受控调用方注入，角色路径由产品强制为
   中央固定路径）；
5. 复核步骤 2-4 的读取来源**未发生并发漂移**（漂移即拒绝，不刷新来源、不改旧 attempt）；
6. ``prepare_dsh_launch``（固定 ``argv`` / ``cwd`` / ``env`` 由既有隔离器生成）；
7. 构造 legacy transport（默认 ``orchestrator.dsh.DSHAdapter``；离线测试可注入
   ``transport_factory``）→ 复用 ``DshSdkAdapter`` 的 :class:`DshDevelopmentAdapter`。

注入边界：

- 身份只来自受控入口注入的 ``CallerContext``；本模块不接收 payload 自报身份，也不接收
  调用方提供的“已通过预检”对象。
- transport ``env`` = 经 ``prepare_dsh_launch`` 白名单校验的 env **副本** + 唯一追加键
  ``DSH_SYSTEM_PROMPT``（值 = ``context["systemPrompt"]``）；不继承父进程环境、不读凭据；
  transport 构造之后不再修改 env。
- 只读复用 ``service.registry_read.list_registry_agents`` /
  ``service.dsh_member_preflight.preflight_member`` / ``contracts.development_context`` /
  ``orchestrator.dsh_launch``；环境上限投影未提供，只能按已核 ``environmentId`` +
  ``revision`` 参数化只读 ``SELECT allowed_roots_json, permission_ceiling_json FROM
  environments``；不写第二套 schema、不做迁移、不复制 SQL 口径。

证据等级：本链路是**离线接线**，所有结论停在 SYNTHETIC（夹具）/ UNVERIFIED（声明层），
**不构成**真实身份、OS 隔离、真实模型或可投递性证明。
"""
from __future__ import annotations

import hashlib
import json
import os

from adapters.dsh_sdk import (
    D4_DSH_SDK_AGENT_ID,
    DSH_SDK_PROFILE,
    DSH_SDK_SOURCE_ID,
    DshSdkAdapter,
)
# 只读复用 02A1 已冻结的词法 helper（与 service/registry_read.py 同一用法）。
from contracts.agent_registry import _check_absolute_posix_path, _check_permission_ceiling
from contracts.development_context import (
    ROLE_ID as DEVELOPMENT_ROLE_ID,
    SCHEMA_VERSION as DEVELOPMENT_CONTEXT_SCHEMA_VERSION,
    build_development_context,
)
from contracts.schemas import ContractError
from orchestrator.dsh import DSHAdapter as _LegacyDshTransport
from orchestrator.dsh_launch import prepare_dsh_launch
from service.dsh_member_preflight import preflight_member
from service.registry_read import list_registry_agents

__all__ = [
    "DEVELOPMENT_ADAPTER_SCHEMA_VERSION",
    "HIGH_RISK_PERMISSION_SWITCHES",
    "REQUIRED_ADAPTER_TYPE",
    "REQUIRED_ENVIRONMENT_HOST_TYPE",
    "REQUIRED_EXECUTION_PROFILE_REF",
    "REQUIRED_HOST_AGENT_ID",
    "REQUIRED_MODEL_STRATEGY",
    "SYSTEM_PROMPT_ENV_KEY",
    "DshDevelopmentAdapter",
    "build_development_adapter",
]

# ---------------------------------------------------------------------------
# 受控常量（不可由调用者覆盖；单成员限制如实报告，不改常量）
# ---------------------------------------------------------------------------
DEVELOPMENT_ADAPTER_SCHEMA_VERSION = "dsh-development-adapter.v1"

REQUIRED_ENVIRONMENT_HOST_TYPE = "dsh"
REQUIRED_ADAPTER_TYPE = DSH_SDK_SOURCE_ID        # "dsh-sdk"
REQUIRED_EXECUTION_PROFILE_REF = DSH_SDK_PROFILE  # "sdk-minimal"
REQUIRED_HOST_AGENT_ID = D4_DSH_SDK_AGENT_ID      # "dsh-sdk-exec-001"
REQUIRED_MODEL_STRATEGY = "fixed"

SYSTEM_PROMPT_ENV_KEY = "DSH_SYSTEM_PROMPT"
HIGH_RISK_PERMISSION_SWITCHES = (
    "allowNetwork",
    "allowBusinessWrite",
    "allowGit",
    "allowPublish",
)

# 局部错误码（只进 details.dshDevCode，不写冻结 ERROR_CODES）
DSH_DEV_PREFLIGHT_NOT_ALLOWED = "DSH_DEV_PREFLIGHT_NOT_ALLOWED"
DSH_DEV_MEMBER_BINDING_INVALID = "DSH_DEV_MEMBER_BINDING_INVALID"
DSH_DEV_TASK_BINDING_INVALID = "DSH_DEV_TASK_BINDING_INVALID"
DSH_DEV_ROOTS_NOT_COVERED = "DSH_DEV_ROOTS_NOT_COVERED"
DSH_DEV_CEILING_NOT_COVERED = "DSH_DEV_CEILING_NOT_COVERED"
DSH_DEV_CONTEXT_INVALID = "DSH_DEV_CONTEXT_INVALID"
DSH_DEV_SOURCE_DRIFT = "DSH_DEV_SOURCE_DRIFT"
DSH_DEV_ENV_CONFLICT = "DSH_DEV_ENV_CONFLICT"

# 系统目录别名固定例外：仅这两对，按（字面路径, realpath）全等匹配。
_SYSTEM_ALIAS_TARGETS = {"/tmp": "/private/tmp", "/var": "/private/var"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------
def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reject_input(code, field, reason):
    """受控拒绝：``invalid_task`` + 稳定 ``field`` / ``reason``，不回显文件正文。"""
    raise ContractError(
        "invalid_task",
        message="DSH 开发 Adapter 接线拒绝：%s（field=%s）" % (reason, field),
        details={"dshDevCode": code, "field": field, "reason": reason},
    )


class _JsonReject(Exception):
    """JSON 重复键 / NaN / Infinity 的内部哨兵，不向外泄漏。"""


def _pairs_hook(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise _JsonReject()
        seen.add(key)
    return dict(pairs)


def _reject_constant(_name):
    raise _JsonReject()


def _strict_json_value(text, field):
    """严格解码受控 JSON 列：拒重复键 / NaN / Infinity / 坏语法；不修改输入。"""
    if not isinstance(text, str):
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, field, "json_column_not_text")
    try:
        return json.loads(text, object_pairs_hook=_pairs_hook,
                          parse_constant=_reject_constant)
    except _JsonReject:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, field, "json_column_invalid")
    except (TypeError, ValueError):
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, field, "json_column_invalid")


def _strict_json_object(text, field):
    value = _strict_json_value(text, field)
    if not isinstance(value, dict):
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, field, "json_column_not_object")
    return value


def _absolute_lexical(path, field):
    """严格绝对 POSIX 路径校验（拒相对、``..``、``.``、空段、尾斜杠、NUL/控制字符）。"""
    try:
        return _check_absolute_posix_path(path, field)
    except ContractError:
        _reject_input(DSH_DEV_ROOTS_NOT_COVERED, field, "path_not_absolute_posix")


def _reject_symlink_segments(path, field):
    """逐段拒绝用户符号链接段（含父目录段）；仅放行固定系统别名对。"""
    parts = [part for part in path.split("/") if part]
    for index in range(len(parts)):
        prefix = "/" + "/".join(parts[: index + 1])
        if os.path.islink(prefix):
            expected = _SYSTEM_ALIAS_TARGETS.get(prefix)
            if expected is None or os.path.realpath(prefix) != expected:
                _reject_input(DSH_DEV_ROOTS_NOT_COVERED, field, "path_symlink_rejected")


def _validate_roots(value, field):
    """``allowedRoots``：非空、唯一、严格绝对 POSIX、无用户符号链接段。"""
    if not isinstance(value, list) or not value:
        _reject_input(DSH_DEV_ROOTS_NOT_COVERED, field, "roots_not_non_empty_list")
    seen = set()
    for index, item in enumerate(value):
        position = "%s[%d]" % (field, index)
        text = _absolute_lexical(item, position)
        _reject_symlink_segments(text, position)
        if text in seen:
            _reject_input(DSH_DEV_ROOTS_NOT_COVERED, position, "roots_duplicate")
        seen.add(text)
    return list(value)


def _validate_ceiling(value, field):
    """``permissionCeiling``：严格 5 字段形态 + 四个高风险开关显式 false + 写范围无软链接段。"""
    try:
        _check_permission_ceiling(value, ("/",))
    except ContractError:
        _reject_input(DSH_DEV_CEILING_NOT_COVERED, field, "ceiling_shape_invalid")
    for key in HIGH_RISK_PERMISSION_SWITCHES:
        if value[key] is not False:
            _reject_input(DSH_DEV_CEILING_NOT_COVERED, "%s.%s" % (field, key),
                          "high_risk_switch_not_explicitly_false")
    for index, item in enumerate(value["writeScope"]):
        _reject_symlink_segments(item, "%s.writeScope[%d]" % (field, index))
    return value


def _within(path, root):
    """按路径段判断包含（不做 realpath，避免解析掉符号链接信息后放宽边界）。"""
    if root == "/":
        return True
    return path == root or path.startswith(root + "/")


def _covered_by(roots, path):
    return any(_within(path, root) for root in roots)


def _assert_roots_cover(roots, paths, field):
    for path in paths:
        if not _covered_by(roots, path):
            _reject_input(DSH_DEV_ROOTS_NOT_COVERED, field, "path_outside_allowed_roots")


def _assert_ceiling_covers(ceiling, task_permissions, field):
    """ceiling 必须覆盖任务实际权限：写范围逐项被包含，四个开关不得比任务更窄。"""
    for path in task_permissions["writeScope"]:
        if not _covered_by(ceiling["writeScope"], path):
            _reject_input(DSH_DEV_CEILING_NOT_COVERED, field + ".writeScope",
                          "task_write_scope_outside_ceiling")
    for key in HIGH_RISK_PERMISSION_SWITCHES:
        if task_permissions[key] and not ceiling[key]:
            _reject_input(DSH_DEV_CEILING_NOT_COVERED, "%s.%s" % (field, key),
                          "ceiling_narrower_than_task")


# ---------------------------------------------------------------------------
# 任务实际权限（来自显式任务对象；四个高风险开关必须显式 false）
# ---------------------------------------------------------------------------
def _task_permissions(task, workspace):
    raw = task.get("permissions_json")
    if not isinstance(raw, str):
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.permissions_json",
                      "permissions_json_not_text")
    value = _strict_json_value(raw, "task.permissions_json")
    if not isinstance(value, dict):
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.permissions_json",
                      "permissions_json_not_object")
    required = ("writeScope",) + HIGH_RISK_PERMISSION_SWITCHES
    for key in required:
        if key not in value:
            _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.permissions." + key,
                          "permission_field_missing")
    for key in HIGH_RISK_PERMISSION_SWITCHES:
        if value[key] is not False:
            _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.permissions." + key,
                          "high_risk_switch_not_explicitly_false")

    scope = value["writeScope"]
    if not isinstance(scope, list) or not scope:
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.permissions.writeScope",
                      "write_scope_not_non_empty_list")
    resolved = []
    seen = set()
    for index, entry in enumerate(scope):
        position = "task.permissions.writeScope[%d]" % index
        if not isinstance(entry, str) or not entry:
            _reject_input(DSH_DEV_TASK_BINDING_INVALID, position, "write_scope_entry_invalid")
        if ".." in entry.split("/"):
            _reject_input(DSH_DEV_TASK_BINDING_INVALID, position, "write_scope_dotdot_segment")
        if entry.startswith("/"):
            candidate = entry
        else:
            candidate = workspace.rstrip("/") + "/" + entry.lstrip("/")
        candidate = candidate.rstrip("/") or "/"
        text = _absolute_lexical(candidate, position)
        _reject_symlink_segments(text, position)
        if text in seen:
            _reject_input(DSH_DEV_TASK_BINDING_INVALID, position, "write_scope_duplicate")
        seen.add(text)
        resolved.append(text)
    permissions = {"writeScope": resolved}
    for key in HIGH_RISK_PERMISSION_SWITCHES:
        permissions[key] = value[key]
    return permissions


# ---------------------------------------------------------------------------
# 只读读取：成员上限（复用 list_registry_agents）与环境上限（参数化只读 SELECT）
# ---------------------------------------------------------------------------
def _read_member_limits(conn, registry_id, agent_id):
    projection = list_registry_agents(conn, registry_id=registry_id)
    for item in projection["items"]:
        if item["agentId"] == agent_id:
            return {
                "allowedRoots": _validate_roots(item["allowedRoots"], "member.allowedRoots"),
                "permissionCeiling": _validate_ceiling(
                    item["permissionCeiling"], "member.permissionCeiling"),
            }
    _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.agentId", "member_missing_in_projection")


def _read_environment(conn, environment):
    """按已核 ``environmentId`` + ``revision`` 参数化只读读环境上限（不写第二套 schema）。"""
    environment_id = environment.get("environmentId")
    revision = environment.get("revision")
    if not isinstance(environment_id, str) or not environment_id:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.environment.environmentId",
                      "environment_id_invalid")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.environment.revision",
                      "environment_revision_invalid")
    row = conn.execute(
        "SELECT allowed_roots_json, permission_ceiling_json FROM environments "
        "WHERE environment_id = ? AND revision = ?",
        (environment_id, revision),
    ).fetchone()
    if row is None:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.environment",
                      "environment_row_missing")
    return {
        "environmentId": environment_id,
        "revision": revision,
        "allowedRoots": _validate_roots(
            _strict_json_value(row[0], "environment.allowedRoots"),
            "environment.allowedRoots"),
        "permissionCeiling": _validate_ceiling(
            _strict_json_object(row[1], "environment.permissionCeiling"),
            "environment.permissionCeiling"),
    }


# ---------------------------------------------------------------------------
# 成员预检与一致性核对
# ---------------------------------------------------------------------------
def _run_preflight(conn, registry_id, caller, request, evaluated_at):
    return preflight_member(conn, registry_id=registry_id, caller=caller,
                            request=request, evaluated_at=evaluated_at)


def _require_allowed(check):
    if check.get("decision") != "allow":
        raise ContractError(
            "permission_denied",
            message="成员预检未放行，零 transport 构造（decision=%s）" % check.get("decision"),
            details={
                "dshDevCode": DSH_DEV_PREFLIGHT_NOT_ALLOWED,
                "decision": check.get("decision"),
                "reasonCodes": [reason["code"] for reason in check.get("reasons", [])],
            },
        )
    member = check.get("member")
    project = check.get("project")
    if not isinstance(member, dict) or not isinstance(project, dict):
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "preflight", "preflight_projection_missing")
    return member, project


def _assert_member_binding(member):
    environment = member.get("environment") or {}
    binding = member.get("binding") or {}
    model = member.get("modelProfile") or {}
    if environment.get("hostType") != REQUIRED_ENVIRONMENT_HOST_TYPE:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.environment.hostType",
                      "host_type_not_dsh")
    if environment.get("adapterType") != REQUIRED_ADAPTER_TYPE:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.environment.adapterType",
                      "environment_adapter_type_not_dsh_sdk")
    if binding.get("adapterType") != REQUIRED_ADAPTER_TYPE:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.binding.adapterType",
                      "binding_adapter_type_not_dsh_sdk")
    if binding.get("executionProfileRef") != REQUIRED_EXECUTION_PROFILE_REF:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.binding.executionProfileRef",
                      "execution_profile_not_sdk_minimal")
    if binding.get("hostAgentId") != REQUIRED_HOST_AGENT_ID:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.binding.hostAgentId",
                      "host_agent_id_not_controlled_constant")
    if model.get("strategy") != REQUIRED_MODEL_STRATEGY:
        _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.modelProfile.strategy",
                      "model_strategy_not_fixed")
    for key in ("provider", "modelId"):
        value = model.get(key)
        if not isinstance(value, str) or not value.strip():
            _reject_input(DSH_DEV_MEMBER_BINDING_INVALID, "member.modelProfile." + key,
                          "fixed_model_field_empty")


def _assert_task_binding(task, check, member, project, workspace):
    if task.get("project_id") != check["request"]["projectId"]:
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.project_id",
                      "task_project_not_preflighted")
    if project.get("projectId") != task.get("project_id"):
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.project_id",
                      "task_project_not_associated")
    if task.get("agent_id") != member["agentId"]:
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.agent_id",
                      "task_agent_not_preflighted")
    declared = task.get("workspace")
    if not isinstance(declared, str) or not declared.startswith("/"):
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.workspace",
                      "task_workspace_must_be_absolute")
    if os.path.realpath(declared) != os.path.realpath(workspace):
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task.workspace",
                      "task_workspace_mismatch")


# ---------------------------------------------------------------------------
# 开发上下文与启动规格
# ---------------------------------------------------------------------------
def _assert_context_shape(context):
    if not isinstance(context, dict):
        _reject_input(DSH_DEV_CONTEXT_INVALID, "developmentContext", "context_not_object")
    if context.get("schemaVersion") != DEVELOPMENT_CONTEXT_SCHEMA_VERSION:
        _reject_input(DSH_DEV_CONTEXT_INVALID, "developmentContext.schemaVersion",
                      "context_schema_version_mismatch")
    if context.get("role") != DEVELOPMENT_ROLE_ID:
        _reject_input(DSH_DEV_CONTEXT_INVALID, "developmentContext.role",
                      "context_role_not_central_skill_worker")
    if not isinstance(context.get("systemPrompt"), str) or not context["systemPrompt"]:
        _reject_input(DSH_DEV_CONTEXT_INVALID, "developmentContext.systemPrompt",
                      "context_system_prompt_missing")
    digest = context.get("contextDigest")
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        _reject_input(DSH_DEV_CONTEXT_INVALID, "developmentContext.contextDigest",
                      "context_digest_missing")
    sources = context.get("sources")
    if not isinstance(sources, list) or len(sources) != 3:
        _reject_input(DSH_DEV_CONTEXT_INVALID, "developmentContext.sources",
                      "context_sources_not_three")
    kinds = [item.get("kind") for item in sources if isinstance(item, dict)]
    if kinds != ["role", "project", "taskCard"]:
        _reject_input(DSH_DEV_CONTEXT_INVALID, "developmentContext.sources",
                      "context_sources_kinds_invalid")


def _env_overrides(value):
    """只接受 locale/timezone/terminal 覆盖；不允许注入 ``DSH_SYSTEM_PROMPT``。"""
    if value is None:
        return None
    if isinstance(value, dict) and SYSTEM_PROMPT_ENV_KEY in value:
        _reject_input(DSH_DEV_ENV_CONFLICT, "envOverrides." + SYSTEM_PROMPT_ENV_KEY,
                      "system_prompt_env_not_overridable")
    return value


def _transport_env(spec, context):
    """在**已验证** env 副本上追加唯一固定键 ``DSH_SYSTEM_PROMPT``；不继承父进程环境。"""
    env = getattr(spec, "env", None)
    if not isinstance(env, dict) or not env.get("HOME"):
        _reject_input(DSH_DEV_ENV_CONFLICT, "launch.env", "launch_env_missing_home")
    transport_env = dict(env)
    if SYSTEM_PROMPT_ENV_KEY in transport_env:
        _reject_input(DSH_DEV_ENV_CONFLICT, "launch.env." + SYSTEM_PROMPT_ENV_KEY,
                      "system_prompt_key_already_present")
    transport_env[SYSTEM_PROMPT_ENV_KEY] = context["systemPrompt"]
    return transport_env


# ---------------------------------------------------------------------------
# 并发漂移复核（构造 transport 之前）
# ---------------------------------------------------------------------------
def _source_fingerprint(check, member_limits, environment):
    return _sha256_text(_canonical_json({
        "registry": check["registry"],
        "decision": check["decision"],
        "member": check["member"],
        "project": check["project"],
        "checks": check["checks"],
        "checksSatisfied": check["checksSatisfied"],
        "memberLimits": member_limits,
        "environment": environment,
    }))


def _assert_no_source_drift(conn, registry_id, caller, request, evaluated_at, expected):
    recheck = _run_preflight(conn, registry_id, caller, request, evaluated_at)
    member, _project = _require_allowed(recheck)
    member_limits = _read_member_limits(conn, registry_id, member["agentId"])
    environment = _read_environment(conn, member["environment"])
    actual = _source_fingerprint(recheck, member_limits, environment)
    if actual != expected:
        raise ContractError(
            "invalid_transition",
            message="构造前复核发现成员/环境/模型/binding 并发漂移，拒绝且零 transport 构造",
            details={"dshDevCode": DSH_DEV_SOURCE_DRIFT},
        )


# ---------------------------------------------------------------------------
# transport 构造
# ---------------------------------------------------------------------------
def _default_transport_factory(*, store_dir, command, cwd, env, provider, model):
    """默认构造 legacy stdio transport（``orchestrator.dsh.DSHAdapter``）。

    只构造，不 ``start`` / 不 ``submit``：本函数不产生子进程。
    """
    return _LegacyDshTransport(store_dir, command, cwd, env, provider=provider, model=model)


def _construct_transport(factory, store_dir, spec, transport_env, provider, model):
    return factory(
        store_dir=str(store_dir),
        command=list(spec.command),
        cwd=spec.cwd,
        env=dict(transport_env),
        provider=provider,
        model=model,
    )


def _setup_report(registry_id, check, context, spec, transport_env):
    return {
        "schemaVersion": DEVELOPMENT_ADAPTER_SCHEMA_VERSION,
        "sourceId": DSH_SDK_SOURCE_ID,
        "agentId": REQUIRED_HOST_AGENT_ID,
        "registryId": registry_id,
        "request": dict(check["request"]),
        "caller": dict(check["caller"]),
        "preflightDecision": check["decision"],
        "contextDigest": context["contextDigest"],
        "sources": [dict(item) for item in context["sources"]],
        "launch": {
            "argv": list(spec.command),
            "cwd": spec.cwd,
            "envDigest": _sha256_text(_canonical_json(transport_env)),
            "systemPromptKey": SYSTEM_PROMPT_ENV_KEY,
            "systemPromptSha256": _sha256_text(context["systemPrompt"]),
        },
        "limits": {
            "singleMemberOnly": True,
            "hostAgentIdFromControlledConstant": True,
            "started": False,
            "submitted": False,
            "createdD4Task": False,
            "createdD4Attempt": False,
            "createdD4Review": False,
            "wouldWrite": False,
            "wouldDispatch": False,
            "hostStarted": False,
            "networkAccessed": False,
            "credentialsAccessed": False,
            "realDshAuthorizationGateUnchanged": True,
            "realDshConnected": False,
            "dispatchable": False,
            "evidenceLevel": "SYNTHETIC",
        },
    }


# ---------------------------------------------------------------------------
# 适配器：复用 DshSdkAdapter，只把开发上下文追加进 prompt
# ---------------------------------------------------------------------------
class DshDevelopmentAdapter(DshSdkAdapter):
    """离线开发链路适配器：复用 ``DshSdkAdapter``，**只**把开发上下文追加进 prompt。

    与父类的唯一行为差异：``_build_prompt`` 在父类消息之后追加一段 canonical JSON
    开发上下文（含 ``contextDigest`` 与三份来源 SHA）。该段随内容一起参与父类的
    ``requestDigest`` 计算，因此同一 attempt 用不同上下文重投会命中父类既有幂等语义
    ``DSH_ATTEMPT_CONFLICT``，旧账本不被覆盖。

    构造时对传入上下文做一次 canonical 快照（``self._context_json``），之后只使用快照：
    构造完成后外部再改原 dict，不会改变本轮的 prompt 或摘要。父类的 capabilities /
    evidenceLevel / name 一律原样继承，不放大任何证据等级。
    """

    development_adapter = True

    def __init__(self, transport, store_dir, *, development_context,
                 setup_report=None, **kwargs):
        if not isinstance(development_context, dict) or not development_context:
            raise ValueError("DSH_DEV_CONTEXT_MUST_BE_NON_EMPTY_OBJECT")
        # 先冻结快照，再调用父类构造：快照不依赖任何 transport 状态。
        self._context_json = _canonical_json(development_context)
        self._frozen_context = json.loads(self._context_json)
        self.context_digest = self._frozen_context.get("contextDigest")
        self.source_shas = {
            item.get("kind"): item.get("sha256")
            for item in (self._frozen_context.get("sources") or [])
            if isinstance(item, dict)
        }
        self.setup_report = dict(setup_report or {})
        super().__init__(transport, store_dir, **kwargs)

    def prompt_context_block(self):
        """追加进 prompt 的上下文块（只读；来自构造时的冻结快照）。"""
        context = self._frozen_context
        return "\n".join([
            "[DSH 开发上下文] schemaVersion=%s role=%s contextDigest=%s" % (
                context.get("schemaVersion"), context.get("role"),
                context.get("contextDigest")),
            "[DSH 开发上下文来源] " + _canonical_json(context.get("sources")),
            "[DSH 开发上下文全文] " + self._context_json,
        ])

    def _build_prompt(self, task, attempt):
        return super()._build_prompt(task, attempt) + "\n" + self.prompt_context_block()


# ---------------------------------------------------------------------------
# 公开工厂
# ---------------------------------------------------------------------------
def build_development_adapter(*, conn, registry_id, caller, request, evaluated_at,
                              task, attempt, workspace, role_path, task_card_path,
                              allowed_task_roots, expected_digests, node, sdk_entry,
                              runtime_roots, state_dir, store_dir,
                              env_overrides=None, transport_factory=None):
    """离线装配 DSH 开发 Adapter：返回**未启动、未投递**的适配器。

    参数（全部由受控调用方注入）：

    - ``conn`` / ``registry_id`` / ``caller`` / ``request`` / ``evaluated_at``：传给它
      ``preflight_member`` 的只读快照与受控身份；本函数自己调用预检，**不接受**调用方
      自带的“已通过预检”对象。
    - ``task`` / ``attempt``：显式任务与执行轮次（``task.permissions_json`` 决定本轮实际权限）。
    - ``workspace`` / ``role_path`` / ``task_card_path`` / ``allowed_task_roots`` /
      ``expected_digests``：传给 ``build_development_context`` 的受控输入。
    - ``node`` / ``sdk_entry`` / ``runtime_roots`` / ``state_dir`` / ``env_overrides``：
      传给 ``prepare_dsh_launch``。
    - ``store_dir``：legacy transport 账本目录与适配器 submissions 目录。
    - ``transport_factory``：**仅**用于离线注入（关键字 ``store_dir`` / ``command`` /
      ``cwd`` / ``env`` / ``provider`` / ``model``）；默认构造 legacy transport。
    - ``env_overrides``：仅 locale/timezone/terminal；不得包含 ``DSH_SYSTEM_PROMPT``。

    任何一步失败都 fail closed：不构造 transport、不启动、不投递，也不写任何 D4 对象。
    """
    if not isinstance(task, dict) or not isinstance(attempt, dict):
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "task", "task_and_attempt_must_be_objects")
    if attempt.get("task_id") != task.get("task_id"):
        _reject_input(DSH_DEV_TASK_BINDING_INVALID, "attempt.task_id", "attempt_not_of_task")
    if transport_factory is not None and not callable(transport_factory):
        raise ValueError("DSH_DEV_TRANSPORT_FACTORY_MUST_BE_CALLABLE")

    # 0) workspace 先按受控词法校验（后续覆盖判断与启动规格都用同一字面路径）。
    workspace_text = _absolute_lexical(workspace, "workspace")
    overrides = _env_overrides(env_overrides)

    # 1) 成员预检：non-allow 或身份非法一律在此拒绝，零 transport 构造。
    check = _run_preflight(conn, registry_id, caller, request, evaluated_at)
    member, project = _require_allowed(check)

    # 2) 成员 / 模型 / binding 与任务一致性。
    _assert_member_binding(member)
    _assert_task_binding(task, check, member, project, workspace_text)

    # 3) 目录与权限上限（成员上限复用 list_registry_agents；环境上限参数化只读 SELECT）。
    task_permissions = _task_permissions(task, workspace_text)
    member_limits = _read_member_limits(conn, registry_id, member["agentId"])
    environment = _read_environment(conn, member["environment"])
    covered_paths = [workspace_text] + list(task_permissions["writeScope"])
    _assert_roots_cover(member_limits["allowedRoots"], covered_paths, "member.allowedRoots")
    _assert_roots_cover(environment["allowedRoots"], covered_paths, "environment.allowedRoots")
    _assert_ceiling_covers(member_limits["permissionCeiling"], task_permissions,
                           "member.permissionCeiling")
    _assert_ceiling_covers(environment["permissionCeiling"], task_permissions,
                           "environment.permissionCeiling")
    _assert_ceiling_covers(project["permissionCeiling"], task_permissions,
                           "project.permissionCeiling")

    # 4) 开发上下文（必须先于启动规格）。
    context = build_development_context(
        workspace=workspace_text,
        task_card_path=task_card_path,
        role_path=role_path,
        allowed_task_roots=allowed_task_roots,
        expected_digests=expected_digests,
    )
    _assert_context_shape(context)

    # 5) 复核来源未并发漂移：紧邻上下文装配之后、启动规格之前（启动规格只由受控
    #    node/sdk/runtime/state 参数决定，不依赖成员/环境读取），漂移即拒绝。
    _assert_no_source_drift(
        conn, registry_id, caller, request, evaluated_at,
        _source_fingerprint(check, member_limits, environment))

    # 6) 启动规格：固定 argv / cwd / env 由既有隔离器生成。
    spec = prepare_dsh_launch(node=node, sdk_entry=sdk_entry, workspace=workspace_text,
                              runtime_roots=runtime_roots, state_dir=state_dir,
                              env=overrides)
    transport_env = _transport_env(spec, context)

    # 7) 构造 legacy transport（默认真实现；离线可注入）并复用 DshSdkAdapter。
    provider = member["modelProfile"]["provider"]
    model = member["modelProfile"]["modelId"]
    factory = transport_factory or _default_transport_factory
    transport = _construct_transport(factory, store_dir, spec, transport_env, provider, model)
    adapter = DshDevelopmentAdapter(
        transport, store_dir,
        development_context=context,
        setup_report=_setup_report(registry_id, check, context, spec, transport_env),
    )
    return adapter
