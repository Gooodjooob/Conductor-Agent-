"""D4-NEXT-02A2：显式候选成员库迁移（接口合同 §13.8，cox 冻结）。

本模块只在调用方**显式调用**时，向调用方提供的 sqlite3.Connection 写入九张 Registry
候选表与 12 个历史保护 trigger，并写入 candidate 元数据；不改默认 SqliteStore、不自动
hook 构造、不新增 CLI/服务、不打开任何库路径。

公开 API（仅这两个）：
- inspect_registry_schema(conn, registry_id=None) -> dict
- apply_registry_candidate_migration(conn, *, registry_id, source_digest) -> dict

设计约束（§13.8）：
- inspect 纯 SQL 只读；不存在元数据返回 unmigrated，不建表/不改时间；形态不完整拒绝，
  不静默修补已标完成但缺表/trigger 的半库。
- apply 显式迁移：校验 ID/digest/foreign_keys/无现有事务；BEGIN IMMEDIATE 内复核元数据
  与 managed 对象，逐句 execute DDL 与元数据，最后 COMMIT；异常 ROLLBACK，保留 SQLite
  异常类型；不使用 executescript（避免隐式 commit 破坏原子性）。
- 旧 schema 对象原地保留，无 DROP/ALTER 旧表。
- 返回体不宣称新成员已验证或 ready 可执行。
"""
from __future__ import annotations

import re
import sqlite3
from datetime import datetime, timezone

from contracts.schemas import ContractError

# ---------------------------------------------------------------------------
# 受控格式（§13.8 cox 冻结补充）
# ---------------------------------------------------------------------------
_REGISTRY_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SOURCE_DIGEST_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

# 九张 Registry 专用表（顺序即 DDL 执行顺序）。
_REGISTRY_TABLE_NAMES = (
    "registry_metadata",
    "environments",
    "model_profiles",
    "registry_agents",
    "agent_bindings",
    "project_agents",
    "agent_checks",
    "attempt_execution_bindings",
    "registry_changes",
)

# 六张不可变历史表：各自 BEFORE UPDATE / BEFORE DELETE 共 12 个 trigger。
_REGISTRY_IMMUTABLE_TABLES = (
    "environments",
    "model_profiles",
    "agent_bindings",
    "agent_checks",
    "attempt_execution_bindings",
    "registry_changes",
)

_IMMUTABLE_MSG = "registry_history_immutable"


def _trigger_name(table, event):
    return "registry_%s_no_%s" % (table, event.lower())


_REGISTRY_TRIGGER_NAMES = tuple(
    _trigger_name(table, event)
    for table in _REGISTRY_IMMUTABLE_TABLES
    for event in ("UPDATE", "DELETE")
)

_REGISTRY_MANAGED_NAMES = frozenset(_REGISTRY_TABLE_NAMES) | frozenset(_REGISTRY_TRIGGER_NAMES)


# ---------------------------------------------------------------------------
# 固定 DDL（九表 + 12 trigger）
#
# 全部写成单行字符串常量；apply 用同一份常量建库，inspect 用同一份常量与
# sqlite_master.sql 做“去分号 + 折叠空白”后的严格比较，防同名空 trigger 或被
# 放宽 CHECK 的半库混入信任。
# ---------------------------------------------------------------------------
_REGISTRY_TABLE_DDL_BASE = (
    (
        "registry_metadata",
        "CREATE TABLE registry_metadata ("
        " singleton INTEGER PRIMARY KEY CHECK (singleton = 1),"
        " registry_id TEXT NOT NULL UNIQUE,"
        " schema_version INTEGER NOT NULL CHECK (typeof(schema_version) = 'integer' AND schema_version = 1),"
        " authority TEXT NOT NULL CHECK (authority = 'candidate'),"
        " revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 1),"
        " source_digest TEXT NOT NULL,"
        " created_at TEXT NOT NULL"
        ")",
    ),
    (
        "environments",
        "CREATE TABLE environments ("
        " environment_id TEXT NOT NULL,"
        " revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 1),"
        " host_type TEXT NOT NULL,"
        " instance_id TEXT NOT NULL,"
        " host_version TEXT,"
        " adapter_type TEXT NOT NULL,"
        " connector_config_ref TEXT NOT NULL,"
        " config_digest TEXT NOT NULL,"
        " allowed_roots_json TEXT NOT NULL,"
        " permission_ceiling_json TEXT NOT NULL,"
        " enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),"
        " created_by_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (environment_id, revision)"
        ")",
    ),
    (
        "model_profiles",
        "CREATE TABLE model_profiles ("
        " model_profile_id TEXT NOT NULL,"
        " profile_revision INTEGER NOT NULL CHECK (typeof(profile_revision) = 'integer' AND profile_revision >= 1),"
        " environment_id TEXT NOT NULL,"
        " environment_revision INTEGER NOT NULL CHECK (typeof(environment_revision) = 'integer' AND environment_revision >= 1),"
        " strategy TEXT NOT NULL CHECK (strategy IN ('fixed', 'host-default')),"
        " provider TEXT,"
        " model_id TEXT,"
        " config_digest TEXT NOT NULL,"
        " created_by_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (model_profile_id, profile_revision),"
        " UNIQUE (model_profile_id, profile_revision, environment_id, environment_revision),"
        " FOREIGN KEY (environment_id, environment_revision)"
        " REFERENCES environments (environment_id, revision),"
        " CHECK ((strategy = 'fixed' AND provider IS NOT NULL AND provider <> ''"
        " AND model_id IS NOT NULL AND model_id <> '')"
        " OR (strategy = 'host-default' AND provider IS NULL AND model_id IS NULL))"
        ")",
    ),
    (
        "registry_agents",
        "CREATE TABLE registry_agents ("
        " agent_id TEXT NOT NULL PRIMARY KEY,"
        " agent_alias TEXT NOT NULL,"
        " normalized_alias TEXT NOT NULL UNIQUE,"
        " display_name TEXT NOT NULL,"
        " roles_json TEXT NOT NULL,"
        " capabilities_json TEXT NOT NULL,"
        " environment_id TEXT NOT NULL,"
        " environment_revision INTEGER NOT NULL CHECK (typeof(environment_revision) = 'integer' AND environment_revision >= 1),"
        " allowed_roots_json TEXT NOT NULL,"
        " permission_ceiling_json TEXT NOT NULL,"
        " model_profile_id TEXT NOT NULL,"
        " model_profile_revision INTEGER NOT NULL CHECK (typeof(model_profile_revision) = 'integer' AND model_profile_revision >= 1),"
        " current_binding_revision INTEGER NOT NULL CHECK (typeof(current_binding_revision) = 'integer' AND current_binding_revision >= 1),"
        " enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),"
        " registration_complete INTEGER NOT NULL CHECK (registration_complete = 1),"
        " revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 1),"
        " source_row_json TEXT,"
        " created_by_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL,"
        " FOREIGN KEY (agent_id) REFERENCES agents (agent_id),"
        " FOREIGN KEY (model_profile_id, model_profile_revision, environment_id, environment_revision)"
        " REFERENCES model_profiles (model_profile_id, profile_revision, environment_id, environment_revision),"
        " FOREIGN KEY (agent_id, current_binding_revision, environment_id, environment_revision)"
        " REFERENCES agent_bindings (agent_id, binding_revision, environment_id, environment_revision)"
        " DEFERRABLE INITIALLY DEFERRED"
        ")",
    ),
    (
        "agent_bindings",
        "CREATE TABLE agent_bindings ("
        " agent_id TEXT NOT NULL,"
        " binding_revision INTEGER NOT NULL CHECK (typeof(binding_revision) = 'integer' AND binding_revision >= 1),"
        " environment_id TEXT NOT NULL,"
        " environment_revision INTEGER NOT NULL CHECK (typeof(environment_revision) = 'integer' AND environment_revision >= 1),"
        " adapter_type TEXT NOT NULL,"
        " connector_config_ref TEXT NOT NULL,"
        " execution_profile_ref TEXT,"
        " host_agent_id TEXT,"
        " config_digest TEXT NOT NULL,"
        " created_by_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " PRIMARY KEY (agent_id, binding_revision),"
        " UNIQUE (agent_id, binding_revision, environment_id, environment_revision),"
        " FOREIGN KEY (agent_id) REFERENCES registry_agents (agent_id) DEFERRABLE INITIALLY DEFERRED,"
        " FOREIGN KEY (environment_id, environment_revision)"
        " REFERENCES environments (environment_id, revision),"
        " CHECK ((execution_profile_ref IS NOT NULL AND execution_profile_ref <> '')"
        " OR (host_agent_id IS NOT NULL AND host_agent_id <> ''))"
        ")",
    ),
    (
        "project_agents",
        "CREATE TABLE project_agents ("
        " project_id TEXT NOT NULL,"
        " agent_id TEXT NOT NULL,"
        " project_roles_json TEXT NOT NULL,"
        " permission_ceiling_json TEXT NOT NULL,"
        " enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),"
        " revision INTEGER NOT NULL CHECK (typeof(revision) = 'integer' AND revision >= 1),"
        " created_by_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " updated_at TEXT NOT NULL,"
        " PRIMARY KEY (project_id, agent_id),"
        " FOREIGN KEY (agent_id) REFERENCES registry_agents (agent_id)"
        ")",
    ),
    (
        "agent_checks",
        "CREATE TABLE agent_checks ("
        " check_id TEXT NOT NULL PRIMARY KEY,"
        " agent_id TEXT NOT NULL,"
        " binding_revision INTEGER NOT NULL CHECK (typeof(binding_revision) = 'integer' AND binding_revision >= 1),"
        " environment_id TEXT NOT NULL,"
        " environment_revision INTEGER NOT NULL CHECK (typeof(environment_revision) = 'integer' AND environment_revision >= 1),"
        " config_digest TEXT NOT NULL,"
        " model_profile_id TEXT,"
        " model_profile_revision INTEGER CHECK (model_profile_revision IS NULL"
        " OR (typeof(model_profile_revision) = 'integer' AND model_profile_revision >= 1)),"
        " check_type TEXT NOT NULL CHECK (check_type IN ('config', 'identity', 'login', 'model', 'runtime')),"
        " status TEXT NOT NULL CHECK (status IN ('passed', 'failed', 'unverified')),"
        " evidence_level TEXT NOT NULL CHECK (evidence_level IN ('OFFICIAL', 'LOCAL', 'SYNTHETIC', 'UNVERIFIED')),"
        " observed_at TEXT NOT NULL,"
        " expires_at TEXT NOT NULL,"
        " evidence_ref TEXT NOT NULL,"
        " failure_reason TEXT,"
        " requested_json TEXT NOT NULL,"
        " observed_json TEXT,"
        " created_by_json TEXT NOT NULL,"
        " FOREIGN KEY (agent_id, binding_revision, environment_id, environment_revision)"
        " REFERENCES agent_bindings (agent_id, binding_revision, environment_id, environment_revision),"
        " FOREIGN KEY (model_profile_id, model_profile_revision, environment_id, environment_revision)"
        " REFERENCES model_profiles (model_profile_id, profile_revision, environment_id, environment_revision),"
        " CHECK ((model_profile_id IS NULL AND model_profile_revision IS NULL)"
        " OR (model_profile_id IS NOT NULL AND model_profile_revision IS NOT NULL)),"
        " CHECK (check_type <> 'model' OR model_profile_id IS NOT NULL)"
        ")",
    ),
    (
        "attempt_execution_bindings",
        "CREATE TABLE attempt_execution_bindings ("
        " attempt_id TEXT NOT NULL PRIMARY KEY,"
        " agent_id TEXT NOT NULL,"
        " binding_revision INTEGER NOT NULL CHECK (typeof(binding_revision) = 'integer' AND binding_revision >= 1),"
        " environment_id TEXT NOT NULL,"
        " environment_revision INTEGER NOT NULL CHECK (typeof(environment_revision) = 'integer' AND environment_revision >= 1),"
        " model_profile_id TEXT NOT NULL,"
        " model_profile_revision INTEGER NOT NULL CHECK (typeof(model_profile_revision) = 'integer' AND model_profile_revision >= 1),"
        " source_card_digest TEXT NOT NULL,"
        " workspace TEXT NOT NULL,"
        " permissions_json TEXT NOT NULL,"
        " check_ids_json TEXT NOT NULL,"
        " resolved_config_digest TEXT NOT NULL,"
        " requested_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL,"
        " FOREIGN KEY (attempt_id) REFERENCES attempts (attempt_id),"
        " FOREIGN KEY (agent_id, binding_revision, environment_id, environment_revision)"
        " REFERENCES agent_bindings (agent_id, binding_revision, environment_id, environment_revision),"
        " FOREIGN KEY (model_profile_id, model_profile_revision, environment_id, environment_revision)"
        " REFERENCES model_profiles (model_profile_id, profile_revision, environment_id, environment_revision)"
        ")",
    ),
    (
        "registry_changes",
        "CREATE TABLE registry_changes ("
        " change_id TEXT NOT NULL PRIMARY KEY,"
        " idempotency_key TEXT NOT NULL UNIQUE,"
        " content_digest TEXT NOT NULL,"
        " action TEXT NOT NULL,"
        " object_type TEXT NOT NULL,"
        " object_id TEXT NOT NULL,"
        " before_revision INTEGER CHECK (before_revision IS NULL"
        " OR (typeof(before_revision) = 'integer' AND before_revision >= 1)),"
        " after_revision INTEGER CHECK (after_revision IS NULL"
        " OR (typeof(after_revision) = 'integer' AND after_revision >= 1)),"
        " caller_json TEXT NOT NULL,"
        " result_json TEXT NOT NULL,"
        " created_at TEXT NOT NULL"
        ")",
    ),
)


# ---------------------------------------------------------------------------
# 九表 ID 列非空约束（§13.8 原合同：「TEXT主键需NOT NULL；ID非空」）
#
# required：必须非空且非空字符串（在 NOT NULL 列上再补 `<> ''`）；
# nullable：允许 NULL，但非 NULL 时不得为空串（`IS NULL OR <> ''`，避免 SQLite
#           三值逻辑把 NULL 当成“通过”偷过 CHECK）。
# 该清单是九表 DDL 里 ID 非空 CHECK 的唯一来源，保证“系统核查”覆盖每一个 ID 列，
# 不遗漏、不靠服务事后拦截，也不额外猜 ID 格式（registry_id 仍由代码严格完整匹配）。
# ---------------------------------------------------------------------------
_REGISTRY_REQUIRED_ID_COLUMNS = (
    ("registry_metadata", "registry_id"),
    ("environments", "environment_id"),
    ("environments", "instance_id"),
    ("model_profiles", "model_profile_id"),
    ("model_profiles", "environment_id"),
    ("registry_agents", "agent_id"),
    ("registry_agents", "environment_id"),
    ("registry_agents", "model_profile_id"),
    ("agent_bindings", "agent_id"),
    ("agent_bindings", "environment_id"),
    ("project_agents", "project_id"),
    ("project_agents", "agent_id"),
    ("agent_checks", "check_id"),
    ("agent_checks", "agent_id"),
    ("agent_checks", "environment_id"),
    ("attempt_execution_bindings", "attempt_id"),
    ("attempt_execution_bindings", "agent_id"),
    ("attempt_execution_bindings", "environment_id"),
    ("attempt_execution_bindings", "model_profile_id"),
    ("registry_changes", "change_id"),
    ("registry_changes", "object_id"),
)

_REGISTRY_NULLABLE_ID_COLUMNS = (
    ("model_profiles", "model_id"),
    ("agent_bindings", "host_agent_id"),
    ("agent_checks", "model_profile_id"),
)


def _id_checks_for(table):
    """某表需追加的 ID 非空 CHECK 子句（顺序固定：先 required 再 nullable）。"""
    clauses = []
    for owner, column in _REGISTRY_REQUIRED_ID_COLUMNS:
        if owner == table:
            clauses.append("CHECK (%s <> '')" % column)
    for owner, column in _REGISTRY_NULLABLE_ID_COLUMNS:
        if owner == table:
            clauses.append("CHECK (%s IS NULL OR %s <> '')" % (column, column))
    return clauses


def _with_id_checks(table, ddl):
    """在固定 DDL 结尾追加该表的 ID 非空 CHECK；表名固定、结尾必为 ')'。"""
    clauses = _id_checks_for(table)
    if not clauses:
        return ddl
    if not ddl.endswith(")"):
        raise AssertionError("unexpected DDL shape for table %s" % table)
    return ddl[:-1] + ", " + ", ".join(clauses) + ")"


# 固定 DDL = 冻结基表定义 + 系统化追加的 ID 非空 CHECK（inspect/apply 用同一份）。
_REGISTRY_TABLE_DDL = tuple(
    (name, _with_id_checks(name, sql)) for name, sql in _REGISTRY_TABLE_DDL_BASE
)


def _build_trigger_ddl():
    statements = []
    for table in _REGISTRY_IMMUTABLE_TABLES:
        for event in ("UPDATE", "DELETE"):
            name = _trigger_name(table, event)
            sql = (
                "CREATE TRIGGER %s BEFORE %s ON %s "
                "BEGIN SELECT RAISE(ABORT, '%s'); END"
                % (name, event, table, _IMMUTABLE_MSG)
            )
            statements.append((name, sql))
    return tuple(statements)


_REGISTRY_TRIGGER_DDL = _build_trigger_ddl()


def _migration_statements():
    """按顺序执行的 DDL 计划：九表 + 12 trigger。

    返回 [(object_name, create_sql), ...]。测试可用 mock.patch.object 覆盖本函数，
    在序列中注入非法 SQL，验证中途失败整体回滚（非公开入参，仅内部 hook）。
    """
    return list(_REGISTRY_TABLE_DDL) + list(_REGISTRY_TRIGGER_DDL)


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _norm_sql(sql):
    """sqlite_master.sql 规范化：去分号 + 折叠空白，供严格比较。"""
    if sql is None:
        return None
    return " ".join(sql.replace(";", " ").split())


def _canonical_norm():
    mapping = {}
    for name, sql in _REGISTRY_TABLE_DDL:
        mapping[name] = ("table", _norm_sql(sql))
    for name, sql in _REGISTRY_TRIGGER_DDL:
        mapping[name] = ("trigger", _norm_sql(sql))
    return mapping


def _invalid(field, reason):
    """受控拒绝：既有错误码 invalid_task + 稳定 details.field/reason（不新增全局码）。"""
    return ContractError("invalid_task", details={"field": field, "reason": reason})


def _validate_registry_id(value):
    if not isinstance(value, str) or not _REGISTRY_ID_PATTERN.fullmatch(value):
        raise _invalid("registryId", "invalid_registry_id")


def _validate_source_digest(value):
    # 完整匹配，尾 LF 不 strip。
    if not isinstance(value, str) or not _SOURCE_DIGEST_PATTERN.fullmatch(value):
        raise _invalid("sourceDigest", "invalid_source_digest")


def _foreign_keys_on(conn):
    """返回 foreign_keys pragma 是否开启。

    不捕获 sqlite3.Error：PRAGMA 的授权/IO/SQL 运行错误必须保持原类型向上传播，
    不得被改写成配置类拒绝（如 foreign_keys_off）。PRAGMA 确实返回 off 时，
    由调用方按配置拒绝。
    """
    row = conn.execute("PRAGMA foreign_keys").fetchone()
    return row is not None and row[0] == 1


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _write_metadata(conn, registry_id, source_digest, created_at):
    """写入 candidate 元数据 singleton 行。测试可 patch 本函数模拟写失败。"""
    conn.execute(
        "INSERT INTO registry_metadata "
        "(singleton, registry_id, schema_version, authority, revision, source_digest, created_at) "
        "VALUES (1, ?, 1, 'candidate', 1, ?, ?)",
        (registry_id, source_digest, created_at),
    )


def _scan_managed(conn):
    """返回 sqlite_master 中**所有**对象类型的 {name: [(type, sql), ...]}。

    不按 type 过滤、不按 name 覆盖：同名 table/view/index/trigger 必须全部保留，
    供上层识别同名 managed 冲突。这样既不会把同名 view/index 当成干净的
    unmigrated 库放行，也不会因 dict 覆盖同名不同 type 而漏查。
    """
    rows = conn.execute("SELECT type, name, sql FROM sqlite_master").fetchall()
    found = {}
    for type_, name, sql in rows:
        found.setdefault(name, []).append((type_, sql))
    return found


def _present_managed(found):
    return sorted(name for name in found if name in _REGISTRY_MANAGED_NAMES)


def _integrity_or_raise(found, canonical):
    """完整校验九表 + 12 trigger 的存在、类型与（规范化后）SQL 严格一致。

    同名多对象（table 与 view/index/trigger 同名）视为受控冲突拒绝，绝不静默放行。
    """
    for name in _REGISTRY_TABLE_NAMES:
        entries = found.get(name)
        if not entries:
            raise _invalid(name, "missing_table")
        if len(entries) != 1:
            raise _invalid(name, "object_type_mismatch")
        type_, sql = entries[0]
        if type_ != "table":
            raise _invalid(name, "object_type_mismatch")
        if _norm_sql(sql) != canonical[name][1]:
            raise _invalid(name, "table_definition_mismatch")
    for name in _REGISTRY_TRIGGER_NAMES:
        entries = found.get(name)
        if not entries:
            raise _invalid(name, "missing_trigger")
        if len(entries) != 1:
            raise _invalid(name, "object_type_mismatch")
        type_, sql = entries[0]
        if type_ != "trigger":
            raise _invalid(name, "object_type_mismatch")
        if _norm_sql(sql) != canonical[name][1]:
            raise _invalid(name, "trigger_definition_mismatch")


def _inspect_state(conn, registry_id=None, source_digest=None):
    """分类当前库：{"state": "unmigrated"} / {"state": "ready", "meta": {...}}。

    形态不完整 / 高版本 / ID-摘要不符 → 抛受控 invalid_task，绝不静默修补或放行。
    """
    found = _scan_managed(conn)
    canonical = _canonical_norm()

    has_meta = "registry_metadata" in found
    if not has_meta:
        if _present_managed(found):
            # 无元数据却存在任一 Registry 专用对象 → 不完整库，拒绝。
            raise _invalid("registry_metadata", "incomplete_registry")
        return {"state": "unmigrated"}

    # 读 registry_metadata 数据之前，先依据 sqlite_master 形态做受控拒绝：
    # managed 对象缺失/类型不符/同名冲突/DDL 不一致在此明确拒绝，且本步不读表数据，
    # 故 SQLite 授权/IO 运行错误不会被吞掉或改写为 invalid_task。
    _integrity_or_raise(found, canonical)

    # 形态完整后才读元数据；真实 SQL 运行异常（授权/IO/SQL）保持原类型向上传播。
    rows = conn.execute(
        "SELECT singleton, registry_id, schema_version, authority, revision, "
        "source_digest, created_at FROM registry_metadata"
    ).fetchall()

    if len(rows) != 1:
        raise _invalid("registry_metadata", "metadata_not_singleton")
    row = rows[0]
    singleton, rid, schema_version, authority, revision, digest, created_at = (
        row[0], row[1], row[2], row[3], row[4], row[5], row[6],
    )
    if singleton != 1:
        raise _invalid("registry_metadata", "metadata_not_singleton")

    if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
        if isinstance(schema_version, int) and not isinstance(schema_version, bool) and schema_version > 1:
            raise _invalid("schemaVersion", "unsupported_schema_version")
        raise _invalid("schemaVersion", "invalid_schema_version")
    if authority != "candidate":
        raise _invalid("authority", "authority_mismatch")
    if not isinstance(rid, str) or not _REGISTRY_ID_PATTERN.fullmatch(rid):
        raise _invalid("registryId", "invalid_registry_id")
    if not isinstance(digest, str) or not _SOURCE_DIGEST_PATTERN.fullmatch(digest):
        raise _invalid("sourceDigest", "invalid_source_digest")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision != 1:
        raise _invalid("revision", "invalid_revision")
    if not isinstance(created_at, str) or not created_at:
        raise _invalid("createdAt", "invalid_created_at")

    if registry_id is not None and registry_id != rid:
        raise _invalid("registryId", "registry_id_mismatch")
    if source_digest is not None and source_digest != digest:
        raise _invalid("sourceDigest", "source_digest_conflict")

    return {
        "state": "ready",
        "meta": {"registryId": rid, "sourceDigest": digest, "createdAt": created_at},
    }


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def inspect_registry_schema(conn, registry_id=None):
    """只读检查候选库形态。

    - 未迁移：status=unmigrated / schemaVersion=0 / registryId=null / authority=null。
    - 已迁移：status=ready / schemaVersion=1 / registryId / authority=candidate /
      sourceDigest / revision / createdAt。
    不建表、不改时间、不改 pragma；形态不完整或高版本 → invalid_task + 稳定 field/reason。
    """
    if registry_id is not None:
        _validate_registry_id(registry_id)
    state = _inspect_state(conn, registry_id=registry_id)
    if state["state"] == "unmigrated":
        return {
            "status": "unmigrated",
            "schemaVersion": 0,
            "registryId": None,
            "authority": None,
            "sourceDigest": None,
            "revision": None,
            "createdAt": None,
        }
    meta = state["meta"]
    return {
        "status": "ready",
        "schemaVersion": 1,
        "registryId": meta["registryId"],
        "authority": "candidate",
        "sourceDigest": meta["sourceDigest"],
        "revision": 1,
        "createdAt": meta["createdAt"],
    }


def apply_registry_candidate_migration(conn, *, registry_id, source_digest):
    """显式、原子地把候选 Registry 九表 + 12 trigger 迁移进调用方连接。

    - 只接受既有 sqlite3.Connection；不接路径、不打开库。
    - foreign_keys 必须已 ON；拒绝已有 caller 事务；无元数据而已有任一 Registry 对象先拒绝冲突。
    - BEGIN IMMEDIATE 内复核元数据/形态，逐句 execute DDL 与元数据，最后 COMMIT；
      异常 ROLLBACK（仅回滚本函数拥有的事务），保留 SQLite 异常类型，不用 executescript。
    - 已有同 registryId+schemaVersion+sourceDigest → 只读返回 replayed=True。
    返回 schemaVersion=1 / authority=candidate / registryId / replayed。
    """
    _validate_registry_id(registry_id)
    _validate_source_digest(source_digest)
    if not isinstance(conn, sqlite3.Connection):
        raise _invalid("connection", "connection_required")
    if conn.in_transaction:
        raise _invalid("connection", "connection_in_transaction")
    if not _foreign_keys_on(conn):
        raise _invalid("foreign_keys", "foreign_keys_off")

    started = False
    try:
        conn.execute("BEGIN IMMEDIATE")
        started = True

        state = _inspect_state(conn, registry_id=registry_id, source_digest=source_digest)
        if state["state"] == "ready":
            meta = state["meta"]
            conn.execute("COMMIT")
            started = False
            return {
                "status": "ready",
                "schemaVersion": 1,
                "registryId": meta["registryId"],
                "authority": "candidate",
                "sourceDigest": meta["sourceDigest"],
                "revision": 1,
                "createdAt": meta["createdAt"],
                "replayed": True,
            }

        for _name, sql in _migration_statements():
            conn.execute(sql)
        created_at = _now_iso()
        _write_metadata(conn, registry_id, source_digest, created_at)
        conn.execute("COMMIT")
        started = False
        return {
            "status": "ready",
            "schemaVersion": 1,
            "registryId": registry_id,
            "authority": "candidate",
            "sourceDigest": source_digest,
            "revision": 1,
            "createdAt": created_at,
            "replayed": False,
        }
    except Exception:
        if started and conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                # 清理（ROLLBACK）自身失败不得以成功或配置错误遮蔽原始异常，也不得
                # 吞掉首个异常；这里只保留并继续抛出原异常，保持 fail closed。
                pass
        raise
