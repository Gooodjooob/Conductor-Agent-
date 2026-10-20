"""D4-NEXT-02B1A：候选成员列表只读服务（接口合同 §13.10，cox 冻结）。

只新增本模块与 ``tests/test_d4_registry_read.py``。不实现成员详情、CLI / HTTP、
写服务、配置、检查有效期判定，不迁移、不改名册权威、不打开任何库路径。

公开 API（仅一个）::

    list_registry_agents(conn, *, registry_id, host=None, role=None, enabled=None)

设计约束（§13.10）：

- caller 提供既有 ``sqlite3.Connection``：不 connect / close、不接库路径、不初始化
  SqliteStore、不迁移；只接受默认 tuple 或 ``sqlite3.Row`` 的 ``row_factory``，
  其他 row_factory 拒绝且不改连接配置。
- 只做只读投影：``wouldWrite=false`` / ``wouldDispatch=false`` /
  ``authorityUnchanged=true``；不因 enabled / active / passed 显示 online / ready，
  SYNTHETIC+passed 仍显示 SYNTHETIC；不读 evidenceRef 目标，不调用 clock。
- 先严格校验入参（不执行任何查询），再自有 ``BEGIN``（deferred 只读事务）→
  ``inspect_registry_schema`` 同快照 → 全体成员及其可达引用 / 所投影字段校验 →
  AND 过滤 → ``COMMIT``；失败只 ROLLBACK 自身事务，保留原 SQLite 异常类型。
- 顺序：全体成员先完整校验（含被过滤掉的成员），过滤不能掩盖损坏数据。
- 只读 import 复用已冻结的 candidate schema（``storage.registry_migrations``）与 02A1
  纯合同（``contracts.agent_registry``）helper；不复制整个迁移器 / validator，
  不修改任何旧模块。
"""
from __future__ import annotations

import json
import math
import sqlite3

from contracts.agent_registry import (
    CAPABILITY_VOCABULARY,
    ROLE_VOCABULARY,
    normalize_agent_alias,
)
# 只读复用 02A1 已冻结私有词法 helper（§13.9 允许的只读 import），不改旧模块。
from contracts.agent_registry import (
    _ID_RE,
    _SHA256_RE,
    _check_absolute_posix_path,
    _check_permission_ceiling,
    _check_string,
)
from contracts.schemas import ContractError

import storage.registry_migrations as _migrations
from storage.registry_migrations import inspect_registry_schema

# ---------------------------------------------------------------------------
# 常量（§13.10 冻结）
# ---------------------------------------------------------------------------
READ_SCHEMA_VERSION = "registry-agent-list.v1"

# 投影 JSON：UTF-8 ≤ 1 MiB（与 02A1 parse 上限一致）。
_JSON_MAX_BYTES = 1024 * 1024

# 字符串上限与 02A1 一致：通用 128，displayName 256。
_TEXT_MAX_LEN = 128
_DISPLAY_NAME_MAX_LEN = 256
_FAILURE_REASON_MAX_LEN = 256

# 检查枚举（§13.8 DDL CHECK）。
_CHECK_TYPES = ("config", "identity", "login", "model", "runtime")
_CHECK_STATUSES = ("passed", "failed", "unverified")
_EVIDENCE_LEVELS = ("OFFICIAL", "LOCAL", "SYNTHETIC", "UNVERIFIED")

# model strategy（§13.8）。
_MODEL_STRATEGIES = ("fixed", "host-default")

# 冻结的拒绝原因（§13.10），不新增全局 code。
REASON_INVALID_ARGUMENT = "invalid_argument"
REASON_CONNECTION_REQUIRED = "connection_required"
REASON_UNSUPPORTED_ROW_FACTORY = "unsupported_row_factory"
REASON_CONNECTION_IN_TRANSACTION = "connection_in_transaction"
REASON_REGISTRY_UNMIGRATED = "registry_unmigrated"
REASON_MISSING_REFERENCE = "missing_reference"
REASON_REFERENCE_MISMATCH = "reference_mismatch"
REASON_INVALID_PERSISTED_VALUE = "invalid_persisted_value"


# ---------------------------------------------------------------------------
# 内部工具：受控拒绝与字段路径
# ---------------------------------------------------------------------------
def _invalid(field, reason):
    """统一失败出口：既有 ContractError(invalid_task) + 稳定 field/reason。"""
    raise ContractError(
        "invalid_task",
        message="成员列表只读服务拒绝：%s（field=%s）" % (reason, field),
        details={"field": field, "reason": reason},
    )


def _field(agent_id, *parts):
    """``agents.<agentId>.<camelCase 投影路径>``。"""
    return ".".join(("agents.%s" % agent_id,) + parts)


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


def _reject_non_finite_float(text):
    """parse_float 钩子：``1e999`` / ``-1e999`` 之类指数溢出解码成非有限 float 即拒绝。

    字面 ``NaN`` / ``Infinity`` / ``-Infinity`` 由 ``parse_constant`` 拦截；这里补上
    “文本有限但解码后溢出”的路径（R1 F3）。正常 1e308 / -1.5 等有限浮点原值保留。
    """
    value = float(text)
    if not math.isfinite(value):
        raise _JsonReject()
    return value


def _check_finite(value):
    """完整递归兜底：任意 object / list 嵌套层中的非有限 float 都拒绝。

    字符串 ``"1e999"`` / ``"Infinity"`` 是字面字符串，不是 float，保持原值。
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _JsonReject()
        return
    if isinstance(value, dict):
        for item in value.values():
            _check_finite(item)
        return
    if isinstance(value, list):
        for item in value:
            _check_finite(item)


def _decode_json(text, field):
    """解码所投影 JSON：严格 UTF-8 ≤ 1 MiB，拒重复键 / 非有限数 / 过深 / 坏语法。

    非有限值覆盖两条路径：字面 ``NaN`` / ``Infinity``（parse_constant）与指数溢出
    ``1e999`` / ``-1e999``（parse_float + 递归校验）；字段路径保留，不回显 JSON 正文。
    """
    if not isinstance(text, str):
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    try:
        encoded = len(text.encode("utf-8"))
    except UnicodeEncodeError:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    if encoded > _JSON_MAX_BYTES:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_constant=_reject_constant,
            parse_float=_reject_non_finite_float,
        )
        _check_finite(value)
        return value
    except _JsonReject:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    except RecursionError:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    except (ValueError, TypeError):
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)


def _json_list(text, field):
    value = _decode_json(text, field)
    if not isinstance(value, list):
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    return value


def _json_object(text, field):
    value = _decode_json(text, field)
    if not isinstance(value, dict):
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    return value


# -- 词法校验（复用 §13.7 冻结规则） -----------------------------------------
def _check_id(value, field):
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)


def _check_optional_id(value, field):
    if value is None:
        return
    _check_id(value, field)


def _check_digest(value, field):
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)


def _check_revision(value, field):
    """revision 必须严格正整数，bool 不算 int。"""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)


def _check_stored_bool(value, field):
    """存储 0/1 严格转 bool；其他值（含 bool / 2 / 字符串）拒绝。"""
    if isinstance(value, bool) or not isinstance(value, int) or value not in (0, 1):
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    return value == 1


def _check_text(value, field, max_len=_TEXT_MAX_LEN):
    try:
        _check_string(value, field, max_len)
    except ContractError:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)


def _check_optional_text(value, field, max_len=_TEXT_MAX_LEN):
    if value is None:
        return
    _check_text(value, field, max_len)


def _check_path(value, field):
    try:
        _check_absolute_posix_path(value, field)
    except ContractError:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)


def _check_vocab_list(value, field, vocabulary, allow_empty):
    if not isinstance(value, list):
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    if not value and not allow_empty:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    seen = set()
    for item in value:
        if not isinstance(item, str) or item not in vocabulary:
            _invalid(field, REASON_INVALID_PERSISTED_VALUE)
        if item in seen:
            _invalid(field, REASON_INVALID_PERSISTED_VALUE)
        seen.add(item)


def _check_root_list(value, field):
    """allowedRoots：非空、唯一、严格绝对 POSIX 路径（§13.7 词法）。"""
    if not isinstance(value, list) or not value:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    seen = set()
    for item in value:
        _check_path(item, field)
        if item in seen:
            _invalid(field, REASON_INVALID_PERSISTED_VALUE)
        seen.add(item)


def _check_ceiling(value, field):
    """permissionCeiling 形态校验（§13.10：沿用 §13.7 词法/形态，这是声明不是核验）。

    只校验形态与“业务 / Git / 发布必须 false”，不以 writeScope 与 allowedRoots 的
    包含关系扩大拒绝范围（本卡只冻结词法/形态，包含关系由受信写入服务保证）。
    """
    try:
        _check_permission_ceiling(value, ("/",))
    except ContractError:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)


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
    """registryId：§13.7 受控 ID 完整匹配，不省略 / null / 空 / 尾 LF。"""
    if not isinstance(value, str) or _migrations._REGISTRY_ID_PATTERN.fullmatch(value) is None:
        _invalid("registryId", REASON_INVALID_ARGUMENT)


def _validate_host(value):
    """host：null 或同规则非空 hostType 精确值，不猜宿主类型。"""
    if value is None:
        return None
    if not isinstance(value, str) or _ID_RE.fullmatch(value) is None:
        _invalid("host", REASON_INVALID_ARGUMENT)
    return value


def _validate_role(value):
    """role：null 或 ROLE_VOCABULARY 精确项。"""
    if value is None:
        return None
    if not isinstance(value, str) or value not in ROLE_VOCABULARY:
        _invalid("role", REASON_INVALID_ARGUMENT)
    return value


def _validate_enabled(value):
    """enabled：null 或严格 bool，拒绝 0 / 1 / 字符串。"""
    if value is None:
        return None
    if not isinstance(value, bool):
        _invalid("enabled", REASON_INVALID_ARGUMENT)
    return value


# ---------------------------------------------------------------------------
# 读取与组装（全部在自有只读事务内）
# ---------------------------------------------------------------------------
def _one(conn, sql, params):
    row = conn.execute(sql, params).fetchone()
    return None if row is None else tuple(row)


def _exists(conn, sql, params):
    return conn.execute(sql, params).fetchone() is not None


_MEMBER_SQL = (
    "SELECT agent_id, agent_alias, normalized_alias, display_name, roles_json, "
    "capabilities_json, environment_id, environment_revision, allowed_roots_json, "
    "permission_ceiling_json, model_profile_id, model_profile_revision, "
    "current_binding_revision, enabled, registration_complete, revision, "
    "created_at, updated_at FROM registry_agents"
)

_ENV_SQL = (
    "SELECT environment_id, revision, host_type, instance_id, host_version, "
    "adapter_type, connector_config_ref, config_digest, enabled FROM environments "
    "WHERE environment_id = ? AND revision = ?"
)

_BINDING_SQL = (
    "SELECT agent_id, binding_revision, environment_id, environment_revision, "
    "adapter_type, connector_config_ref, execution_profile_ref, host_agent_id, "
    "config_digest FROM agent_bindings WHERE agent_id = ? AND binding_revision = ?"
)

_MODEL_SQL = (
    "SELECT model_profile_id, profile_revision, environment_id, environment_revision, "
    "strategy, provider, model_id, config_digest FROM model_profiles "
    "WHERE model_profile_id = ? AND profile_revision = ?"
)

_PROJECT_SQL = (
    "SELECT project_id, project_roles_json, permission_ceiling_json, enabled, "
    "revision, created_at, updated_at FROM project_agents WHERE agent_id = ?"
)

_CHECK_SQL = (
    "SELECT check_id, agent_id, binding_revision, environment_id, environment_revision, "
    "config_digest, model_profile_id, model_profile_revision, check_type, status, "
    "evidence_level, observed_at, expires_at, evidence_ref, failure_reason, "
    "requested_json, observed_json FROM agent_checks WHERE agent_id = ?"
)


def _load_members(conn):
    """全体成员（不 inner join，避免静默丢坏成员），按 agentId 码点升序。"""
    rows = [tuple(r) for r in conn.execute(_MEMBER_SQL).fetchall()]
    return sorted(rows, key=lambda row: row[0])


def _build_environment(conn, agent_id, env_id, env_rev):
    field = _field(agent_id, "environment")
    row = _one(conn, _ENV_SQL, (env_id, env_rev))
    if row is None:
        _invalid(field, REASON_MISSING_REFERENCE)
    _check_id(row[0], field + ".environmentId")
    _check_revision(row[1], field + ".revision")
    _check_id(row[2], field + ".hostType")
    _check_id(row[3], field + ".instanceId")
    _check_optional_text(row[4], field + ".hostVersion")
    _check_id(row[5], field + ".adapterType")
    _check_id(row[6], field + ".connectorConfigRef")
    _check_digest(row[7], field + ".configDigest")
    enabled = _check_stored_bool(row[8], field + ".enabled")
    return {
        "environmentId": row[0],
        "revision": row[1],
        "hostType": row[2],
        "instanceId": row[3],
        "hostVersion": row[4],
        "adapterType": row[5],
        "connectorConfigRef": row[6],
        "configDigest": row[7],
        "enabled": enabled,
    }


def _build_binding(conn, agent_id, binding_rev, environment):
    field = _field(agent_id, "binding")
    row = _one(conn, _BINDING_SQL, (agent_id, binding_rev))
    if row is None:
        _invalid(field, REASON_MISSING_REFERENCE)
    _check_id(row[0], field + ".agentId")
    _check_revision(row[1], field + ".bindingRevision")
    _check_id(row[2], field + ".environmentId")
    _check_revision(row[3], field + ".environmentRevision")
    # 环境版本 / adapter / connector 必须与 environment 相符，不择最新版本修补。
    if row[2] != environment["environmentId"] or row[3] != environment["revision"]:
        _invalid(field, REASON_REFERENCE_MISMATCH)
    _check_id(row[4], field + ".adapterType")
    _check_id(row[5], field + ".connectorConfigRef")
    if row[4] != environment["adapterType"] or row[5] != environment["connectorConfigRef"]:
        _invalid(field, REASON_REFERENCE_MISMATCH)
    _check_optional_id(row[6], field + ".executionProfileRef")
    _check_optional_id(row[7], field + ".hostAgentId")
    if row[6] is None and row[7] is None:
        _invalid(field, REASON_INVALID_PERSISTED_VALUE)
    # binding 与 environment 的 configDigest 描述不同对象，允许不同，不判断相等。
    _check_digest(row[8], field + ".configDigest")
    return {
        "agentId": row[0],
        "bindingRevision": row[1],
        "environmentId": row[2],
        "environmentRevision": row[3],
        "adapterType": row[4],
        "connectorConfigRef": row[5],
        "executionProfileRef": row[6],
        "hostAgentId": row[7],
        "configDigest": row[8],
    }


def _build_model_profile(conn, agent_id, mp_id, mp_rev, environment):
    field = _field(agent_id, "modelProfile")
    row = _one(conn, _MODEL_SQL, (mp_id, mp_rev))
    if row is None:
        _invalid(field, REASON_MISSING_REFERENCE)
    _check_id(row[0], field + ".modelProfileId")
    _check_revision(row[1], field + ".profileRevision")
    _check_id(row[2], field + ".environmentId")
    _check_revision(row[3], field + ".environmentRevision")
    if row[2] != environment["environmentId"] or row[3] != environment["revision"]:
        _invalid(field, REASON_REFERENCE_MISMATCH)
    strategy = row[4]
    if strategy not in _MODEL_STRATEGIES:
        _invalid(field + ".strategy", REASON_INVALID_PERSISTED_VALUE)
    if strategy == "fixed":
        _check_id(row[5], field + ".provider")
        _check_id(row[6], field + ".modelId")
    elif row[5] is not None or row[6] is not None:
        # host-default 必须 provider / modelId 都为 null，不猜实际模型。
        _invalid(field + ".provider", REASON_INVALID_PERSISTED_VALUE)
    _check_digest(row[7], field + ".configDigest")
    return {
        "modelProfileId": row[0],
        "profileRevision": row[1],
        "environmentId": row[2],
        "environmentRevision": row[3],
        "strategy": row[4],
        "provider": row[5],
        "modelId": row[6],
        "configDigest": row[7],
    }


def _build_projects(conn, agent_id):
    rows = [tuple(r) for r in conn.execute(_PROJECT_SQL, (agent_id,)).fetchall()]
    projects = []
    for row in rows:
        project_id = row[0]
        field = _field(agent_id, "projects", project_id)
        _check_id(project_id, field + ".projectId")
        _check_vocab_list(
            _json_list(row[1], field + ".roles"),
            field + ".roles",
            ROLE_VOCABULARY,
            allow_empty=False,
        )
        _check_ceiling(_json_object(row[2], field + ".permissionCeiling"), field + ".permissionCeiling")
        enabled = _check_stored_bool(row[3], field + ".enabled")
        _check_revision(row[4], field + ".revision")
        _check_text(row[5], field + ".createdAt")
        _check_text(row[6], field + ".updatedAt")
        projects.append(
            {
                "projectId": project_id,
                "roles": _json_list(row[1], field + ".roles"),
                "permissionCeiling": _json_object(row[2], field + ".permissionCeiling"),
                "enabled": enabled,
                "revision": row[4],
                "createdAt": row[5],
                "updatedAt": row[6],
            }
        )
    return sorted(projects, key=lambda item: item["projectId"])


def _build_checks(conn, agent_id):
    rows = [tuple(r) for r in conn.execute(_CHECK_SQL, (agent_id,)).fetchall()]
    checks = []
    for row in rows:
        check_id = row[0]
        field = _field(agent_id, "checks", check_id)
        _check_id(check_id, field + ".checkId")
        _check_id(row[1], field + ".agentId")
        _check_revision(row[2], field + ".bindingRevision")
        _check_id(row[3], field + ".environmentId")
        _check_revision(row[4], field + ".environmentRevision")
        _check_digest(row[5], field + ".configDigest")

        # 历史 binding：先按主键精确查（agentId + bindingRevision），再核环境关联。
        # 主键不存在才是 missing_reference；主键存在但环境 / 版本与该 binding 不符是
        # reference_mismatch，不能用复合 SELECT 空结果把两者混为一谈（R1 F2）。
        history_binding = _one(conn, _BINDING_SQL, (row[1], row[2]))
        if history_binding is None:
            _invalid(field + ".bindingRevision", REASON_MISSING_REFERENCE)
        if history_binding[2] != row[3]:
            _invalid(field + ".environmentId", REASON_REFERENCE_MISMATCH)
        if history_binding[3] != row[4]:
            _invalid(field + ".environmentRevision", REASON_REFERENCE_MISMATCH)

        # 历史 check 所引用的 environment 精确版本必须真实存在：binding 行存在不代表
        # 它的 environment 存在（成员当前环境完整也不代表历史环境完整，R1 F1）。
        # 只查同 Connection 同快照，不 connect / 迁移 / 选 latest 修补。
        if not _exists(
            conn,
            "SELECT 1 FROM environments WHERE environment_id = ? AND revision = ?",
            (row[3], row[4]),
        ):
            _invalid(field + ".environmentId", REASON_MISSING_REFERENCE)

        mp_id = row[6]
        mp_rev = row[7]
        if (mp_id is None) != (mp_rev is None):
            _invalid(field + ".modelProfileId", REASON_INVALID_PERSISTED_VALUE)
        if mp_id is not None:
            _check_id(mp_id, field + ".modelProfileId")
            _check_revision(mp_rev, field + ".modelProfileRevision")
            model = _one(conn, _MODEL_SQL, (mp_id, mp_rev))
            if model is None:
                _invalid(field + ".modelProfileId", REASON_MISSING_REFERENCE)
            if model[2] != row[3] or model[3] != row[4]:
                _invalid(field + ".modelProfileRevision", REASON_REFERENCE_MISMATCH)

        check_type = row[8]
        status = row[9]
        evidence_level = row[10]
        if check_type not in _CHECK_TYPES:
            _invalid(field + ".checkType", REASON_INVALID_PERSISTED_VALUE)
        if status not in _CHECK_STATUSES:
            _invalid(field + ".status", REASON_INVALID_PERSISTED_VALUE)
        if evidence_level not in _EVIDENCE_LEVELS:
            _invalid(field + ".evidenceLevel", REASON_INVALID_PERSISTED_VALUE)
        if check_type == "model" and mp_id is None:
            _invalid(field + ".checkType", REASON_INVALID_PERSISTED_VALUE)
        _check_text(row[11], field + ".observedAt")
        _check_text(row[12], field + ".expiresAt")
        _check_text(row[13], field + ".evidenceRef")
        _check_optional_text(row[14], field + ".failureReason", _FAILURE_REASON_MAX_LEN)
        requested = _json_object(row[15], field + ".requested")
        observed = None
        if row[16] is not None:
            observed = _json_object(row[16], field + ".observed")
        checks.append(
            {
                "checkId": check_id,
                "agentId": row[1],
                "bindingRevision": row[2],
                "environmentId": row[3],
                "environmentRevision": row[4],
                "configDigest": row[5],
                "modelProfileId": mp_id,
                "modelProfileRevision": mp_rev,
                "checkType": check_type,
                "status": status,
                "evidenceLevel": evidence_level,
                "observedAt": row[11],
                "expiresAt": row[12],
                "evidenceRef": row[13],
                "failureReason": row[14],
                "requested": requested,
                "observed": observed,
            }
        )
    # observedAt 原字符串降序、同值 checkId 码点升序（稳定排序两次实现）。
    checks.sort(key=lambda item: item["checkId"])
    checks.sort(key=lambda item: item["observedAt"], reverse=True)
    return checks


def _build_item(conn, member):
    """校验并组装一个成员（缺引用 / 坏字段一律拒绝，不静默丢成员）。"""
    (
        agent_id,
        agent_alias,
        normalized_alias,
        display_name,
        roles_json,
        capabilities_json,
        env_id,
        env_rev,
        allowed_roots_json,
        ceiling_json,
        mp_id,
        mp_rev,
        binding_rev,
        enabled,
        registration_complete,
        revision,
        created_at,
        updated_at,
    ) = member

    _check_id(agent_id, _field(agent_id, "agentId"))
    # 当前成员必须引用旧 agents 中已有 ID（不从旧 note / MD 猜成员）。
    if not _exists(conn, "SELECT 1 FROM agents WHERE agent_id = ?", (agent_id,)):
        _invalid(_field(agent_id, "agentId"), REASON_MISSING_REFERENCE)

    _check_text(agent_alias, _field(agent_id, "agentAlias"))
    try:
        expected_alias = normalize_agent_alias(agent_alias)
    except ContractError:
        _invalid(_field(agent_id, "agentAlias"), REASON_INVALID_PERSISTED_VALUE)
    if normalized_alias != expected_alias:
        _invalid(_field(agent_id, "normalizedAlias"), REASON_INVALID_PERSISTED_VALUE)

    _check_text(display_name, _field(agent_id, "displayName"), _DISPLAY_NAME_MAX_LEN)
    if not display_name.strip():
        _invalid(_field(agent_id, "displayName"), REASON_INVALID_PERSISTED_VALUE)
    _check_revision(revision, _field(agent_id, "revision"))
    _check_revision(env_rev, _field(agent_id, "environment") + ".revision")
    _check_revision(mp_rev, _field(agent_id, "modelProfile") + ".profileRevision")
    _check_revision(binding_rev, _field(agent_id, "binding") + ".bindingRevision")
    _check_id(mp_id, _field(agent_id, "modelProfile") + ".modelProfileId")
    _check_text(created_at, _field(agent_id, "createdAt"))
    _check_text(updated_at, _field(agent_id, "updatedAt"))

    roles = _json_list(roles_json, _field(agent_id, "roles"))
    _check_vocab_list(roles, _field(agent_id, "roles"), ROLE_VOCABULARY, allow_empty=False)
    capabilities = _json_list(capabilities_json, _field(agent_id, "capabilities"))
    _check_vocab_list(
        capabilities, _field(agent_id, "capabilities"), CAPABILITY_VOCABULARY, allow_empty=True
    )
    allowed_roots = _json_list(allowed_roots_json, _field(agent_id, "allowedRoots"))
    _check_root_list(allowed_roots, _field(agent_id, "allowedRoots"))
    ceiling = _json_object(ceiling_json, _field(agent_id, "permissionCeiling"))
    _check_ceiling(ceiling, _field(agent_id, "permissionCeiling"))

    environment = _build_environment(conn, agent_id, env_id, env_rev)
    binding = _build_binding(conn, agent_id, binding_rev, environment)
    model_profile = _build_model_profile(conn, agent_id, mp_id, mp_rev, environment)

    return {
        "agentId": agent_id,
        "agentAlias": agent_alias,
        "normalizedAlias": normalized_alias,
        "displayName": display_name,
        "roles": roles,
        "capabilities": capabilities,
        "enabled": _check_stored_bool(enabled, _field(agent_id, "enabled")),
        "registrationComplete": _check_stored_bool(
            registration_complete, _field(agent_id, "registrationComplete")
        ),
        "revision": revision,
        "allowedRoots": allowed_roots,
        "permissionCeiling": ceiling,
        "createdAt": created_at,
        "updatedAt": updated_at,
        "environment": environment,
        "binding": binding,
        "modelProfile": model_profile,
        "projects": _build_projects(conn, agent_id),
        "checks": _build_checks(conn, agent_id),
    }


def _matches(item, host, role, enabled):
    """过滤条件全部 AND；过滤只在完整校验之后生效，不掩盖损坏。"""
    if host is not None and item["environment"]["hostType"] != host:
        return False
    if role is not None and role not in item["roles"]:
        return False
    if enabled is not None and item["enabled"] is not enabled:
        return False
    return True


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def list_registry_agents(conn, *, registry_id, host=None, role=None, enabled=None):
    """只读返回 candidate 成员列表（同快照，AND 过滤）。

    返回 ``registry-agent-list.v1``：``schemaVersion`` / ``registry`` / ``filters`` /
    ``wouldWrite=false`` / ``wouldDispatch=false`` / ``authorityUnchanged=true`` /
    ``items``。不授予任何写 / 派发能力，不提升 online / ready，不改 caller 事务、
    连接配置或 row_factory。
    """
    _require_connection(conn)
    _check_row_factory(conn)
    if conn.in_transaction:
        _invalid("connection", REASON_CONNECTION_IN_TRANSACTION)
    _validate_registry_id(registry_id)
    host = _validate_host(host)
    role = _validate_role(role)
    enabled = _validate_enabled(enabled)

    conn.execute("BEGIN")  # deferred 只读事务，只读不升级为写锁。
    try:
        inspection = inspect_registry_schema(conn, registry_id)
        if inspection.get("status") != "ready":
            _invalid("registry", REASON_REGISTRY_UNMIGRATED)

        items = [_build_item(conn, member) for member in _load_members(conn)]
        filtered = [item for item in items if _matches(item, host, role, enabled)]

        result = {
            "schemaVersion": READ_SCHEMA_VERSION,
            "registry": {
                "registryId": inspection["registryId"],
                "schemaVersion": inspection["schemaVersion"],
                "authority": inspection["authority"],
                "revision": inspection["revision"],
                "sourceDigest": inspection["sourceDigest"],
            },
            "filters": {"host": host, "role": role, "enabled": enabled},
            "wouldWrite": False,
            "wouldDispatch": False,
            "authorityUnchanged": True,
            "items": filtered,
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
