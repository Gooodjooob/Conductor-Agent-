"""D4-NEXT-02B1B：候选成员详情只读服务（02B1A 列表服务之后的详情边界，cox 冻结）。

只新增本模块与 ``tests/test_d4_registry_detail.py``。不实现成员写服务、CLI / HTTP、
列表服务、迁移器、配置、检查有效期判定，不迁移、不改名册权威、不打开任何库路径。

公开 API（仅一个）：:

    inspect_registry_agent(conn, *, registry_id, agent_id)

设计约束（本卡 §3）：

- caller 提供既有 ``sqlite3.Connection``：不 connect / close、不接库路径、不初始化
  SqliteStore、不迁移、不改连接配置；只接受默认 tuple 或 ``sqlite3.Row`` 的
  ``row_factory``，其他 row_factory 拒绝且不改配置。
- 只做只读声明投影：``wouldWrite=false`` / ``wouldDispatch=false`` /
  ``authorityUnchanged=true``。不因 enabled / active / passed 推导 online / ready，
  SYNTHETIC + passed 仍显示 SYNTHETIC；不计算检查有效期、不更新 lastSeen、不探测
  宿主、不访问 evidenceRef 目标、不选择默认成员 / 模型、不创建 project、不导出
  Markdown、不切换名册权威。
- 先严格校验 ``registry_id`` / ``agent_id``（不执行任何查询），再自有 ``BEGIN``
  （deferred 只读事务）→ ``inspect_registry_schema`` 同快照 → 单个成员及其**全部**
  可达历史引用校验 → ``COMMIT``；失败只 ROLLBACK 自身事务，不提交或回滚 caller
  事务，保留 SQLite 原异常类型。
- 历史引用按同样强度完整校验：全体历史 binding、所涉精确环境版本、所涉精确
  model profile 版本缺一即拒绝；不用 latest 补齐，不因只查单个成员而跳过该成员的
  历史引用，也不把损坏藏在空白列表里。
- 只读 import 复用 02B1A 已审核 helper（``service.registry_read``）与已冻结的
  candidate schema（``storage.registry_migrations``）/ §13.7 词法常量；不复制
  迁移器 / validator，不修改任何旧模块。
"""
from __future__ import annotations

import sqlite3

from contracts.agent_registry import _ID_RE
from contracts.schemas import ContractError

import storage.registry_migrations as _migrations
from storage.registry_migrations import inspect_registry_schema

# 只读复用 02B1A 已审核的私有校验 helper / SQL（本卡 §3.4），不改旧模块。
from service.registry_read import (
    REASON_CONNECTION_IN_TRANSACTION,
    REASON_CONNECTION_REQUIRED,
    REASON_INVALID_ARGUMENT,
    REASON_MISSING_REFERENCE,
    REASON_REFERENCE_MISMATCH,
    REASON_REGISTRY_UNMIGRATED,
    REASON_UNSUPPORTED_ROW_FACTORY,
    _build_binding,
    _build_environment,
    _build_item,
    _build_model_profile,
    _check_id,
    _check_revision,
    _check_text,
    _field,
)

# ---------------------------------------------------------------------------
# 常量（本卡 §3.5 冻结）
# ---------------------------------------------------------------------------
DETAIL_SCHEMA_VERSION = "registry-agent-detail.v1"

# 单成员行：沿用列表服务同一投影列序，只补 WHERE 精确主键过滤，
# 不引入第二份成员读取口径（也因此不需要重复一次列表服务的列约定）。
_MEMBER_ROW_SQL = (
    "SELECT agent_id, agent_alias, normalized_alias, display_name, roles_json, "
    "capabilities_json, environment_id, environment_revision, allowed_roots_json, "
    "permission_ceiling_json, model_profile_id, model_profile_revision, "
    "current_binding_revision, enabled, registration_complete, revision, "
    "created_at, updated_at FROM registry_agents WHERE agent_id = ?"
)

# 该成员全部历史绑定版本（含当前版本）。比列表服务多投影一行 createdAt 声明字段，
# 不投影 created_by_json（内部记账，不是成员声明）。
_BINDING_HISTORY_SQL = (
    "SELECT agent_id, binding_revision, environment_id, environment_revision, "
    "adapter_type, connector_config_ref, execution_profile_ref, host_agent_id, "
    "config_digest, created_at FROM agent_bindings WHERE agent_id = ?"
)

_ENV_ROW_SQL = (
    "SELECT environment_id, revision, host_type, instance_id, host_version, "
    "adapter_type, connector_config_ref, config_digest, enabled FROM environments "
    "WHERE environment_id = ? AND revision = ?"
)

_BINDING_ROW_SQL = (
    "SELECT agent_id, binding_revision, environment_id, environment_revision, "
    "adapter_type, connector_config_ref, execution_profile_ref, host_agent_id, "
    "config_digest FROM agent_bindings WHERE agent_id = ? AND binding_revision = ?"
)

_MODEL_ROW_SQL = (
    "SELECT model_profile_id, profile_revision, environment_id, environment_revision, "
    "strategy, provider, model_id, config_digest FROM model_profiles "
    "WHERE model_profile_id = ? AND profile_revision = ?"
)


# ---------------------------------------------------------------------------
# 内部工具：受控拒绝
# ---------------------------------------------------------------------------
def _invalid(field, reason):
    """统一失败出口：沿用列表服务冻结的 ContractError(invalid_task) + field/reason。

    code / reason 取值与列表服务逐字一致，只有 message 前缀区分为详情服务，
    便于调用方按同一枚举分支处理。
    """
    raise ContractError(
        "invalid_task",
        message="成员详情只读服务拒绝：%s（field=%s）" % (reason, field),
        details={"field": field, "reason": reason},
    )


def _one(conn, sql, params):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else tuple(row)


def _require_row(conn, sql, params, field):
    """先按详情路径判定引用存在性（主键缺失 = missing_reference），
    形态 / 词法 / 关联一致性仍交给列表服务已审核的 helper，不重写一套校验。
    """
    row = _one(conn, sql, params)
    if row is None:
        _invalid(field, REASON_MISSING_REFERENCE)
    return row


# ---------------------------------------------------------------------------
# 入参校验（错误参数先拒绝，不执行任何查询）
# ---------------------------------------------------------------------------
def _require_connection(conn):
    if not isinstance(conn, sqlite3.Connection):
        _invalid("connection", REASON_CONNECTION_REQUIRED)


def _check_row_factory(conn):
    """只接受默认 tuple（row_factory=None）或 sqlite3.Row，其他拒绝且不改配置。"""
    factory = conn.row_factory
    if factory is not None and factory is not sqlite3.Row:
        _invalid("connection", REASON_UNSUPPORTED_ROW_FACTORY)


def _validate_registry_id(value):
    """registryId：§13.8 受控 ID 完整匹配，不省略 / null / 空 / 尾 LF。"""
    if not isinstance(value, str) or _migrations._REGISTRY_ID_PATTERN.fullmatch(value) is None:
        _invalid("registryId", REASON_INVALID_ARGUMENT)


def _validate_agent_id(value):
    """agentId：§13.7 受控 ID 完整匹配。

    一次完整匹配同时拒绝：非字符串 / None / bool、空串、尾 LF、含 ``/`` 的 POSIX /
    相对路径、含 ``://`` 的 URL、以及 Unicode 控制字符（都不在受控字符集内）。
    """
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _invalid("agentId", REASON_INVALID_ARGUMENT)


# ---------------------------------------------------------------------------
# 历史引用组装（全部在自有只读事务的同快照内）
# ---------------------------------------------------------------------------
def _build_environment_ref(conn, agent_id, env_id, env_rev, environments):
    """取一个精确环境版本；重复引用只 build 一次，缺失则用详情字段路径拒绝。"""
    _check_id(env_id, _field(agent_id, "environmentHistory", str(env_id)) + ".environmentId")
    _check_revision(env_rev, _field(agent_id, "environmentHistory", str(env_id)) + ".revision")
    key = (env_id, env_rev)
    if key in environments:
        return environments[key]
    field = _field(agent_id, "environmentHistory", env_id, str(env_rev))
    _require_row(conn, _ENV_ROW_SQL, key, field)
    # 形态 / 词法 / 已登记字段投影复用已审核 helper；不读凭据或 evidenceRef 目标。
    environment = _build_environment(conn, agent_id, env_id, env_rev)
    environments[key] = environment
    return environment


def _build_binding_history(conn, agent_id, rows, environments):
    """该成员全部历史绑定版本：按 bindingRevision 升序，旧版本不覆盖、不合并。"""
    history = []
    for row in rows:
        binding_rev = row[1]
        env_id = row[2]
        env_rev = row[3]
        field = _field(agent_id, "bindingHistory", str(binding_rev))
        _check_revision(binding_rev, field + ".bindingRevision")
        _check_id(env_id, field + ".environmentId")
        _check_revision(env_rev, field + ".environmentRevision")
        environment = _build_environment_ref(conn, agent_id, env_id, env_rev, environments)
        _require_row(conn, _BINDING_ROW_SQL, (agent_id, binding_rev), field + ".bindingRevision")
        # 环境版本 / adapter / connector 一致性由已审核 helper 判定（reference_mismatch）。
        binding = _build_binding(conn, agent_id, binding_rev, environment)
        _check_text(row[9], field + ".createdAt")
        binding["createdAt"] = row[9]
        history.append(binding)
    history.sort(key=lambda item: item["bindingRevision"])
    return history


def _collect_model_refs(item):
    """当前成员 modelProfile + 全部历史 check 引用的精确 model profile 版本（去重保序）。"""
    refs = []

    def add(mp_id, mp_rev, env_id, env_rev):
        entry = (mp_id, mp_rev, env_id, env_rev)
        if entry not in refs:
            refs.append(entry)

    current = item["modelProfile"]
    add(
        current["modelProfileId"],
        current["profileRevision"],
        current["environmentId"],
        current["environmentRevision"],
    )
    for check in item["checks"]:
        if check["modelProfileId"] is not None:
            add(
                check["modelProfileId"],
                check["modelProfileRevision"],
                check["environmentId"],
                check["environmentRevision"],
            )
    return refs


def _build_model_history(conn, agent_id, refs, environments):
    """所涉全部精确 model profile 版本：按 modelProfileId / profileRevision 稳定排序。"""
    profiles = []
    for mp_id, mp_rev, env_id, env_rev in refs:
        field = _field(agent_id, "modelProfiles", mp_id, str(mp_rev))
        _check_id(mp_id, field + ".modelProfileId")
        _check_revision(mp_rev, field + ".profileRevision")
        environment = _build_environment_ref(conn, agent_id, env_id, env_rev, environments)
        _require_row(conn, _MODEL_ROW_SQL, (mp_id, mp_rev), field)
        # strategy / provider / modelId 形态与「model ↔ 精确环境版本」关联一致性
        # 均由已审核 helper 判定；host-default 保留 null，不猜实际模型。
        profiles.append(_build_model_profile(conn, agent_id, mp_id, mp_rev, environment))
    profiles.sort(key=lambda item: (item["modelProfileId"], item["profileRevision"]))
    return profiles


def _environment_history(environments):
    """按 environmentId + revision 稳定排序的只读环境版本列表。"""
    return sorted(environments.values(), key=lambda item: (item["environmentId"], item["revision"]))


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def inspect_registry_agent(conn, *, registry_id, agent_id):
    """只读返回单个候选成员详情（同快照，含全部历史引用）。

    返回 ``registry-agent-detail.v1``：``schemaVersion`` / ``registry`` /
    ``item`` / ``bindingHistory`` / ``environmentHistory`` / ``modelProfiles`` /
    ``wouldWrite=false`` / ``wouldDispatch=false`` / ``authorityUnchanged=true``。

    ``item`` 是列表服务对该成员的完整当前投影（含 projects 与 checks）；其余三段
    历史只补当前投影未展开的版本线索，不另建第二份可写状态，也不因为只查一个
    成员就放宽该成员自身历史的完整性要求。
    """
    _require_connection(conn)
    _check_row_factory(conn)
    if conn.in_transaction:
        _invalid("connection", REASON_CONNECTION_IN_TRANSACTION)
    _validate_registry_id(registry_id)
    _validate_agent_id(agent_id)

    conn.execute("BEGIN")  # deferred 只读事务，只读不升级为写锁。
    try:
        # 未迁移 / 错 registryId / schema 漂移：原样保留既有 inspect 稳定错误语义。
        inspection = inspect_registry_schema(conn, registry_id)
        if inspection.get("status") != "ready":
            _invalid("registry", REASON_REGISTRY_UNMIGRATED)
        if inspection.get("authority") != "candidate":
            _invalid("registry", REASON_REGISTRY_UNMIGRATED)
        if inspection.get("registryId") != registry_id:
            _invalid("registryId", REASON_REFERENCE_MISMATCH)

        member = _one(conn, _MEMBER_ROW_SQL, (agent_id,))
        if member is None:
            # 目标成员不存在：稳定 field=agentId / reason=missing_reference。
            _invalid("agentId", REASON_MISSING_REFERENCE)

        item = _build_item(conn, member)

        binding_rows = [
            tuple(row) for row in conn.execute(_BINDING_HISTORY_SQL, (agent_id,)).fetchall()
        ]

        environments = {}
        current_environment = item["environment"]
        environments[
            (current_environment["environmentId"], current_environment["revision"])
        ] = current_environment

        binding_history = _build_binding_history(conn, agent_id, binding_rows, environments)
        model_history = _build_model_history(
            conn, agent_id, _collect_model_refs(item), environments
        )

        result = {
            "schemaVersion": DETAIL_SCHEMA_VERSION,
            "registry": {
                "registryId": inspection["registryId"],
                "schemaVersion": inspection["schemaVersion"],
                "authority": inspection["authority"],
                "revision": inspection["revision"],
                "sourceDigest": inspection["sourceDigest"],
            },
            "item": item,
            "bindingHistory": binding_history,
            "environmentHistory": _environment_history(environments),
            "modelProfiles": model_history,
            "wouldWrite": False,
            "wouldDispatch": False,
            "authorityUnchanged": True,
        }
        conn.execute("COMMIT")
        return result
    except Exception:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                # 清理失败不得替代原异常（fail closed），保留并抛出首个异常。
                pass
        raise
