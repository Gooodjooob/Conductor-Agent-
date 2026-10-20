"""D4-NEXT-02B2：候选成员登记服务（预检 + 受信应用最小纵向切片）。

只新增本模块与 ``tests/test_d4_agent_registry_service.py``。不实现 CLI / 宿主探测 /
模型 probe / 迁移正式库 / 名册切换 / 真实派发；不改 02A1 纯合同、02A2 迁移器、
02B1A/02B1B 只读服务与其测试。

公开 API（仅三个）：

- ``register_agent(conn, *, registry_id, caller, payload, idempotency_key,
  apply=False, expected_revision=None)``：成员登记。默认预检（apply=False）；
  ``apply=True`` 对应 CLI ``--apply`` 语义，需要受信 owner 上下文与幂等键。
- ``update_agent(...)``：范围外显式拒绝（更新未实现，本卡不猜语义）。
- ``set_agent_enabled(...)``：范围外显式拒绝（启停未实现，本卡不猜语义）。

设计约束（任务卡 acceptance + 接口合同 §13.2/§13.3/§13.8）：

- 登记默认 ``enabled=false``：payload 由 02A1 冻结 validator 强制首次登记
  ``enabled`` 严格 ``false``；写入 ``registry_agents.enabled`` 一律硬编码 ``0``，
  不接受任何路径把首次登记写成启用。
- 身份只来自受控入口注入的 ``CallerContext``（``service.context``）；payload 自报的
  createdBy / 角色文字不授予任何权限。预检要求存在合法 CallerContext（缺省拒绝）；
  apply 额外要求 ``role == "owner"`` 且具备 ``registry.apply`` 能力。
- 预检（apply=False）：自有 deferred 只读事务内完成全部校验，返回
  ``wouldWrite=false`` / ``wouldDispatch=false`` / ``authorityUnchanged=true``，
  零 DML/DDL：不建表、不更新库、不创建 environments / model_profiles 缺项。
- 未知 environment / model profile 明确拒绝（``unknown_environment`` /
  ``unknown_model_profile``），不静默创建宿主环境或选择默认模型。环境取该
  environmentId 的最新 revision，模型 profile 取该 (modelProfileId, environmentId,
  environment_revision) 的最新 profile_revision；只做精确存在性解析，不做任何
  "任意版本兜底"。
- 重复 ``agentId``（``duplicate_agent``）与重复 ``normalizedAlias``
  （``duplicate_alias``，复用 02A1 NFC→strip→casefold）明确拒绝。
- 幂等：同幂等键 + 同内容摘要返回首次结果（``replayed=true``）；同键不同内容
  拒绝（``idempotency_conflict``）。幂等判定先于冲突检查（§13.2：同键同内容
  返回首次结果）。
- ``expected_revision`` 只属于更新 / 启停语义；登记传非 None 值直接拒绝
  （``expected_revision_not_applicable``）。更新与启停范围未实现，显式拒绝
  （``not_implemented_scope``），不部分执行。
- apply 事务：拒绝 caller 已有事务；``foreign_keys`` 必须 ON；``BEGIN IMMEDIATE``
  内复用 ``inspect_registry_schema`` 复核 candidate authority（不切权威），原子插入
  旧 ``agents`` ID 行（仅缺行时，绝不改写既有行）、``agent_bindings``（revision 1）、
  ``registry_agents`` 与 ``registry_changes`` 审计行，最后 COMMIT；异常只 ROLLBACK
  自有事务，保留 SQLite 原异常类型。
- 历史表（含 ``agent_bindings`` / ``registry_changes``）只追加；不可变由 02A2
  trigger 保证，本服务不做 UPDATE / DELETE。
- 服务不读取 Markdown / 文件 / env / 宿主 / 网络 / 凭据：payload 必须是调用方
  已解析的 dict（Markdown 解析属 CLI 层，不成为第二写入口）；不打开任何库路径，
  只接受既有 ``sqlite3.Connection``；名册权威保持 candidate，不切换正式名册。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from contracts.agent_registry import validate_agent_registration
from contracts.schemas import ContractError

# 只读复用 02B1A 已审核的错误 reason 常量与 service.context 的受控上下文契约；
# 不修改任何旧模块。
from service.context import require_context
from service.registry_read import (
    REASON_CONNECTION_IN_TRANSACTION,
    REASON_CONNECTION_REQUIRED,
    REASON_INVALID_ARGUMENT,
    REASON_REGISTRY_UNMIGRATED,
    REASON_UNSUPPORTED_ROW_FACTORY,
)
from storage.registry_migrations import inspect_registry_schema

# ---------------------------------------------------------------------------
# 常量（本卡冻结）
# ---------------------------------------------------------------------------

RESULT_SCHEMA_VERSION = "registry-agent-register.v1"

# apply 所需的受信 owner 能力；身份与能力均来自受控入口注入的 CallerContext。
REGISTRY_APPLY_CAPABILITY = "registry.apply"
TRUSTED_OWNER_ROLE = "owner"

ACTION_REGISTER = "register"
OBJECT_TYPE_AGENT = "agent"

FIRST_BINDING_REVISION = 1
FIRST_MEMBER_REVISION = 1

# 本模块新增的受控拒绝 reason（沿用既有错误码，不新增全局 code）。
REASON_FOREIGN_KEYS_OFF = "foreign_keys_off"
REASON_UNKNOWN_ENVIRONMENT = "unknown_environment"
REASON_UNKNOWN_MODEL_PROFILE = "unknown_model_profile"
REASON_DUPLICATE_AGENT = "duplicate_agent"
REASON_DUPLICATE_ALIAS = "duplicate_alias"
REASON_IDEMPOTENCY_CONFLICT = "idempotency_conflict"
REASON_EXPECTED_REVISION_NOT_APPLICABLE = "expected_revision_not_applicable"
REASON_NOT_IMPLEMENTED = "not_implemented_scope"

# 受控 ID：完整匹配（与 §13.7 / 02B1A 口径一致）。
_ID_FULLMATCH = "[A-Za-z0-9][A-Za-z0-9_.-]{0,127}"


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _invalid(field, reason):
    """统一失败出口：既有错误码 invalid_task + 稳定 details.field/reason。"""
    raise ContractError(
        "invalid_task",
        message="成员登记服务拒绝：%s（field=%s）" % (reason, field),
        details={"field": field, "reason": reason},
    )


def _denied(message, **details):
    raise ContractError("permission_denied", message=message, details=details)


def _require_connection(conn):
    if not isinstance(conn, sqlite3.Connection):
        _invalid("connection", REASON_CONNECTION_REQUIRED)


def _check_row_factory(conn):
    """只接受默认 tuple（row_factory=None）或 sqlite3.Row，其他拒绝且不改配置。"""
    factory = conn.row_factory
    if factory is not None and factory is not sqlite3.Row:
        _invalid("connection", REASON_UNSUPPORTED_ROW_FACTORY)


def _validate_id(value, field):
    """受控 ID 完整匹配：同时拒绝非字符串 / 空 / 尾 LF / 路径 / URL / 控制字符。"""
    if not isinstance(value, str) or re.fullmatch(_ID_FULLMATCH, value) is None:
        _invalid(field, REASON_INVALID_ARGUMENT)


def _canonical_json(obj):
    """确定性 JSON 序列化：内容摘要 / source_row / 审计 JSON 统一口径。"""
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_digest(validated):
    """登记内容摘要：canonical JSON 的 sha256，前缀 sha256:。"""
    return "sha256:" + hashlib.sha256(_canonical_json(validated).encode("utf-8")).hexdigest()


def _change_id(idempotency_key):
    """审计 change_id：由幂等键确定性派生（同键同 change_id，天然唯一）。"""
    return "chg-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _foreign_keys_on(conn):
    """返回 foreign_keys pragma 是否开启；PRAGMA 运行错误保持原类型向上传播。"""
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    return row is not None and row[0] == 1


def _require_trusted_owner(caller):
    """apply 额外门槛：受信 owner 角色 + registry.apply 能力，缺一即 permission_denied。"""
    if caller.role != TRUSTED_OWNER_ROLE or not caller.has(REGISTRY_APPLY_CAPABILITY):
        _denied(
            "apply 需要受信 owner 上下文与 %s 能力" % REGISTRY_APPLY_CAPABILITY,
            actorId=caller.actor_id,
            role=caller.role,
            required=REGISTRY_APPLY_CAPABILITY,
            entry=caller.entry,
        )


# ---------------------------------------------------------------------------
# 事务内校验（均在自有事务的同快照内执行）
# ---------------------------------------------------------------------------
def _resolve_environment(conn, environment_id):
    """解析 environmentId 的最新 revision；未知环境拒绝，绝不静默创建。"""
    row = conn.execute(
        "SELECT revision FROM environments WHERE environment_id = ? "
        "ORDER BY revision DESC LIMIT 1",
        (environment_id,),
    ).fetchone()
    if row is None:
        _invalid("environmentId", REASON_UNKNOWN_ENVIRONMENT)
    return row[0]


def _resolve_model_profile(conn, model_profile_id, environment_id, environment_revision):
    """解析模型 profile 与已解析环境版本精确匹配的最新 profile_revision。

    不做任意版本兜底：该 (profile, environment, environment_revision) 组合不存在
    即视为未知模型，拒绝（unknown_model_profile）。
    """
    row = conn.execute(
        "SELECT profile_revision FROM model_profiles "
        "WHERE model_profile_id = ? AND environment_id = ? AND environment_revision = ? "
        "ORDER BY profile_revision DESC LIMIT 1",
        (model_profile_id, environment_id, environment_revision),
    ).fetchone()
    if row is None:
        _invalid("modelProfileId", REASON_UNKNOWN_MODEL_PROFILE)
    return row[0]


def _check_registration_conflicts(conn, validated):
    """重复 agentId / 重复 normalizedAlias 明确拒绝（停用别名同样保留占用）。"""
    agent_id = validated["agentId"]
    normalized_alias = validated["normalizedAlias"]
    row = conn.execute(
        "SELECT 1 FROM registry_agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if row is not None:
        _invalid("agentId", REASON_DUPLICATE_AGENT)
    row = conn.execute(
        "SELECT 1 FROM registry_agents WHERE normalized_alias = ?", (normalized_alias,)
    ).fetchone()
    if row is not None:
        _invalid("agentAlias", REASON_DUPLICATE_ALIAS)


def _lookup_idempotency(conn, idempotency_key, digest):
    """查既有幂等键；同键不同内容拒绝，同键同内容返回首审结果 JSON（或 None）。"""
    row = conn.execute(
        "SELECT content_digest, result_json FROM registry_changes "
        "WHERE idempotency_key = ?",
        (idempotency_key,),
    ).fetchone()
    if row is None:
        return None
    stored_digest, stored_result_json = row[0], row[1]
    if stored_digest != digest:
        _invalid("idempotencyKey", REASON_IDEMPOTENCY_CONFLICT)
    return stored_result_json


def _build_result(inspected, validated, resolved, idempotency_key, digest, change_id,
                  *, apply_mode, replayed):
    """唯一返回投影：预检与 apply 共用同一 schema，仅写相关布尔不同。"""
    return {
        "schemaVersion": RESULT_SCHEMA_VERSION,
        "registry": {
            "registryId": inspected["registryId"],
            "schemaVersion": inspected["schemaVersion"],
            "authority": inspected["authority"],
            "revision": inspected["revision"],
            "sourceDigest": inspected["sourceDigest"],
        },
        "wouldWrite": bool(apply_mode),
        "wouldDispatch": False,
        "authorityUnchanged": True,
        "applied": bool(apply_mode),
        "replayed": bool(replayed),
        "agentId": validated["agentId"],
        "normalizedAlias": validated["normalizedAlias"],
        "enabled": False,
        "revision": FIRST_MEMBER_REVISION,
        "resolved": resolved,
        "idempotency": {
            "key": idempotency_key,
            "contentDigest": digest,
            "changeId": change_id,
        },
    }


# ---------------------------------------------------------------------------
# apply 写入（内部步骤单独成函数，供测试定向注入失败验证事务回滚）
# ---------------------------------------------------------------------------
def _insert_legacy_agent(conn, agent_id, display_name, now):
    """旧 agents 表缺行才插入；既有行（label/note/registered_at）绝不改写。"""
    exists = conn.execute(
        "SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)
    ).fetchone()
    if exists is not None:
        return
    conn.execute(
        "INSERT INTO agents (agent_id, label, note, registered_at) VALUES (?,?,?,?)",
        (agent_id, display_name, None, now),
    )


def _insert_binding(conn, validated, resolved, caller_json, now):
    binding = validated["binding"]
    conn.execute(
        "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id, "
        "environment_revision, adapter_type, connector_config_ref, execution_profile_ref, "
        "host_agent_id, config_digest, created_by_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            validated["agentId"],
            resolved["bindingRevision"],
            resolved["environmentId"],
            resolved["environmentRevision"],
            binding["adapterType"],
            binding["connectorConfigRef"],
            binding["executionProfileRef"],
            binding["hostAgentId"],
            binding["configDigest"],
            caller_json,
            now,
        ),
    )


def _insert_member(conn, validated, resolved, caller_json, now):
    """写入 registry_agents：enabled 一律硬编码 0，首次登记严格停用。"""
    conn.execute(
        "INSERT INTO registry_agents (agent_id, agent_alias, normalized_alias, display_name, "
        "roles_json, capabilities_json, environment_id, environment_revision, "
        "allowed_roots_json, permission_ceiling_json, model_profile_id, model_profile_revision, "
        "current_binding_revision, enabled, registration_complete, revision, source_row_json, "
        "created_by_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            validated["agentId"],
            validated["agentAlias"],
            validated["normalizedAlias"],
            validated["displayName"],
            _canonical_json(validated["roles"]),
            _canonical_json(validated["capabilities"]),
            resolved["environmentId"],
            resolved["environmentRevision"],
            _canonical_json(validated["allowedRoots"]),
            _canonical_json(validated["permissionCeiling"]),
            resolved["modelProfileId"],
            resolved["modelProfileRevision"],
            resolved["bindingRevision"],
            0,
            1,
            FIRST_MEMBER_REVISION,
            _canonical_json(validated),
            caller_json,
            now,
            now,
        ),
    )


def _insert_audit(conn, idempotency_key, change_id, digest, agent_id, caller_json,
                  result_json, now):
    conn.execute(
        "INSERT INTO registry_changes (change_id, idempotency_key, content_digest, action, "
        "object_type, object_id, before_revision, after_revision, caller_json, result_json, "
        "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            change_id,
            idempotency_key,
            digest,
            ACTION_REGISTER,
            OBJECT_TYPE_AGENT,
            agent_id,
            None,
            FIRST_MEMBER_REVISION,
            caller_json,
            result_json,
            now,
        ),
    )


def _apply_writes(conn, caller, validated, resolved, idempotency_key, change_id, digest,
                  result, now):
    """原子写入四张表；任何一步失败由调用方整体 ROLLBACK。"""
    caller_json = _canonical_json(caller.to_dict())
    _insert_legacy_agent(conn, validated["agentId"], validated["displayName"], now)
    _insert_binding(conn, validated, resolved, caller_json, now)
    _insert_member(conn, validated, resolved, caller_json, now)
    _insert_audit(
        conn,
        idempotency_key,
        change_id,
        digest,
        validated["agentId"],
        caller_json,
        _canonical_json(result),
        now,
    )


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def register_agent(conn, *, registry_id, caller, payload, idempotency_key,
                   apply=False, expected_revision=None):
    """成员登记：默认预检（零写入），apply=True 受信应用（原子写入候选库）。

    返回唯一投影 dict（``registry-agent-register.v1``）；受控拒绝抛
    ``ContractError``（code=invalid_task / permission_denied + 稳定
    details.field/reason）；SQLite 运行错误保留原类型。
    """
    # 1) 入参先拒绝（不执行任何查询 / 不开事务）。
    _require_connection(conn)
    _check_row_factory(conn)
    if conn.in_transaction:
        _invalid("connection", REASON_CONNECTION_IN_TRANSACTION)
    _validate_id(registry_id, "registryId")
    # 身份只来自受控入口注入的 CallerContext；缺省 / 类型非法默认拒绝。
    require_context(caller, None)
    if apply:
        _require_trusted_owner(caller)
    if expected_revision is not None:
        _invalid("expectedRevision", REASON_EXPECTED_REVISION_NOT_APPLICABLE)
    _validate_id(idempotency_key, "idempotencyKey")
    if not isinstance(payload, dict):
        _invalid("payload", REASON_INVALID_ARGUMENT)
    # 2) 结构校验复用 02A1 冻结纯合同（非法登记 / enabled=true / 未知字段在此拒绝）。
    validated = validate_agent_registration(payload)
    digest = _content_digest(validated)
    # 3) apply 需要 FK 生效以保住 registry_agents -> agents / bindings 的外键关系。
    if apply and not _foreign_keys_on(conn):
        _invalid("foreign_keys", REASON_FOREIGN_KEYS_OFF)

    # 4) 自有事务：预检 deferred 只读；apply BEGIN IMMEDIATE。
    started = False
    try:
        conn.execute("BEGIN IMMEDIATE" if apply else "BEGIN")
        started = True

        # candidate authority 复核：unmigrated / registryId 不符 / 非 candidate 拒绝，
        # 绝不切权威（authorityUnchanged=true）。
        inspected = inspect_registry_schema(conn, registry_id=registry_id)
        if inspected["status"] != "ready":
            _invalid("registry", REASON_REGISTRY_UNMIGRATED)

        env_revision = _resolve_environment(conn, validated["environmentId"])
        profile_revision = _resolve_model_profile(
            conn,
            validated["modelProfileId"],
            validated["environmentId"],
            env_revision,
        )
        resolved = {
            "environmentId": validated["environmentId"],
            "environmentRevision": env_revision,
            "modelProfileId": validated["modelProfileId"],
            "modelProfileRevision": profile_revision,
            "bindingRevision": FIRST_BINDING_REVISION,
        }
        change_id = _change_id(idempotency_key)

        # 5) 幂等先于冲突检查：同键同内容返回首次结果；同键不同内容拒绝。
        stored_result_json = _lookup_idempotency(conn, idempotency_key, digest)
        if stored_result_json is not None:
            if apply:
                conn.execute("COMMIT")
                started = False
                replayed = json.loads(stored_result_json)
                replayed["replayed"] = True
                return replayed
            # 预检对已应用键保持 wouldWrite=false / applied=false，只标记 replayed。
            preview = _build_result(
                inspected, validated, resolved, idempotency_key, digest, change_id,
                apply_mode=False, replayed=True,
            )
            conn.execute("COMMIT")
            started = False
            return preview

        _check_registration_conflicts(conn, validated)

        # 6) 预检到此为止（零 DML）；apply 原子写入并落审计。
        if apply:
            now = _now_iso()
            result = _build_result(
                inspected, validated, resolved, idempotency_key, digest, change_id,
                apply_mode=True, replayed=False,
            )
            _apply_writes(
                conn, caller, validated, resolved, idempotency_key, change_id,
                digest, result, now,
            )
            conn.execute("COMMIT")
            started = False
            return result

        preview = _build_result(
            inspected, validated, resolved, idempotency_key, digest, change_id,
            apply_mode=False, replayed=False,
        )
        conn.execute("COMMIT")
        started = False
        return preview
    except Exception:
        # 只回滚本函数拥有的事务；不提交 / 不回滚 caller 事务；
        # ROLLBACK 自身失败不遮蔽原始异常（保持 fail closed，与 02A2 同口径）。
        if started and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise


def update_agent(conn, *, registry_id, caller, agent_id, payload, idempotency_key,
                 expected_revision, apply=False):
    """成员更新：02B2 范围未实现，显式拒绝（不猜语义、不部分执行）。"""
    raise ContractError(
        "invalid_task",
        message="成员登记服务拒绝：更新范围未实现（field=action）",
        details={"field": "action", "reason": REASON_NOT_IMPLEMENTED},
    )


def set_agent_enabled(conn, *, registry_id, caller, agent_id, enabled,
                      expected_revision, apply=False):
    """成员启停：02B2 范围未实现，显式拒绝（不猜语义、不部分执行）。"""
    raise ContractError(
        "invalid_task",
        message="成员登记服务拒绝：启停范围未实现（field=action）",
        details={"field": "action", "reason": REASON_NOT_IMPLEMENTED},
    )
