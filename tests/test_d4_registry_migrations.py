"""D4-NEXT-02A2 定向测试：显式候选库迁移（接口合同 §13.8）。

运行（cox 独立运行，工作目录 shared/agent-orchestrator）：
    python3 -B -m unittest discover -s tests -p test_d4_registry_migrations.py

范围：
- 只新增本测试文件与 storage/registry_migrations.py；不改 SqliteStore / __init__ / CLI / schema。
- 只测本卡新生成的 legacy 合成 fixture；不读写历史 fixture/正式库/development.sqlite。
- 每例用本轮 runtime（TemporaryDirectory 沿 TMPDIR）内的新库。
"""
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import storage.registry_migrations as rm
from contracts.schemas import ContractError
from storage.registry_migrations import (
    apply_registry_candidate_migration,
    inspect_registry_schema,
)
from storage.sqlite_store import SqliteStore

TS = "2026-10-08T00:00:00+08:00"
DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
REGISTRY_ID = "reg-1"

_LEGACY_TABLES = (
    "sources", "agents", "tasks", "attempts", "events", "deliveries",
    "reviews", "activity", "reconciliation", "conflicts", "reports", "task_source_cards",
)

_MANAGED = set(rm._REGISTRY_MANAGED_NAMES)
_MANAGED_TABLES = set(rm._REGISTRY_TABLE_NAMES)


def runtime_root():
    """本轮隔离 runtime；默认沿 TMPDIR，可用 D4_REGISTRY_RUNTIME_DIR 覆盖。"""
    base = os.environ.get("D4_REGISTRY_RUNTIME_DIR")
    root = Path(base) if base else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


def _open(path):
    """新 raw 连接：autocommit（isolation_level=None）+ FK ON + busy_timeout。"""
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _make_legacy(path):
    """用现有 SqliteStore 生成 legacy schema + 合法合成旧行（不含任何 Registry 对象）。"""
    store = SqliteStore(path)
    c = store.conn
    c.execute("INSERT INTO sources (source_id, label, registered_at) VALUES ('src-1','本地来源',?)", (TS,))
    c.execute("INSERT INTO agents (agent_id, label, note, registered_at) VALUES ('dev-1','成员','registry entry',?)", (TS,))
    c.execute("INSERT INTO tasks (task_id, project_id, title, status, revision, created_at, updated_at) "
              "VALUES ('task-legacy-1','d4-product','旧任务','closed',1,?,?)", (TS, TS))
    c.execute("INSERT INTO attempts (attempt_id, task_id, ordinal, state, created_at) "
              "VALUES ('att-legacy-1','task-legacy-1',1,'delivered',?)", (TS,))
    c.execute("INSERT INTO events (event_id, task_id, attempt_id, sequence, type, occurred_at, received_at) "
              "VALUES ('evt-legacy-1','task-legacy-1','att-legacy-1',1,'task.started',?,?)", (TS, TS))
    c.execute("INSERT INTO deliveries (delivery_id, task_id, attempt_id, summary, created_at) "
              "VALUES ('dlv-legacy-1','task-legacy-1','att-legacy-1','旧交付',?)", (TS,))
    c.execute("INSERT INTO reviews (review_id, task_id, delivery_id, decision, created_at) "
              "VALUES ('rev-legacy-1','task-legacy-1','dlv-legacy-1','approved',?)", (TS,))
    c.execute("INSERT INTO activity (task_id, attempt_id, at, kind) VALUES ('task-legacy-1','att-legacy-1',?,'note')", (TS,))
    c.execute("INSERT INTO reports (report_id, report_revision, task_id) VALUES ('rep-legacy-1',1,'task-legacy-1')")
    c.execute("INSERT INTO task_source_cards (task_id, project_id, card_id, card_revision) "
              "VALUES ('task-legacy-1','d4-product','card-1',1)")
    return store


def _dump_table(conn, name):
    try:
        rows = conn.execute("SELECT * FROM %s" % name).fetchall()
    except sqlite3.Error:
        return None
    return [tuple(r) for r in rows]


def _dump_legacy(conn):
    return {t: _dump_table(conn, t) for t in _LEGACY_TABLES}


def _dump_old_master(conn):
    """非 Registry 的旧 sqlite_master 对象（含旧表/旧索引），排除 Registry 表上的自动索引。"""
    out = []
    for r in conn.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall():
        if r[1] in _MANAGED:
            continue
        if r[2] in _MANAGED_TABLES:
            continue
        out.append((r[0], r[1], r[2], r[3]))
    return out


def _names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")}


# -- 合法行插入 helper -------------------------------------------------------
def _insert_env(conn, env_id="env-1", rev=1, instance_id="inst-1"):
    conn.execute(
        "INSERT INTO environments (environment_id, revision, host_type, instance_id, host_version,"
        " adapter_type, connector_config_ref, config_digest, allowed_roots_json, permission_ceiling_json,"
        " enabled, created_by_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (env_id, rev, "local", instance_id, None, "workbuddy", "conn-1", DIGEST_A, "[]", "{}", 1, "{}", TS),
    )


def _insert_model(conn, mp_id="mp-1", env_id="env-1", strategy="fixed",
                  provider="synthetic-provider", model_id="synthetic-model", mp_rev=1, env_rev=1):
    conn.execute(
        "INSERT INTO model_profiles (model_profile_id, profile_revision, environment_id, environment_revision,"
        " strategy, provider, model_id, config_digest, created_by_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mp_id, mp_rev, env_id, env_rev, strategy, provider, model_id, DIGEST_A, "{}", TS),
    )


def _insert_member(conn, agent_id, normalized_alias=None, env_id="env-1", mp_id="mp-1",
                   env_rev=1, mp_rev=1, binding_rev=1, enabled=1, revision=1):
    if normalized_alias is None:
        normalized_alias = agent_id
    conn.execute(
        "INSERT INTO registry_agents (agent_id, agent_alias, normalized_alias, display_name, roles_json,"
        " capabilities_json, environment_id, environment_revision, allowed_roots_json, permission_ceiling_json,"
        " model_profile_id, model_profile_revision, current_binding_revision, enabled, registration_complete,"
        " revision, created_by_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, agent_id, normalized_alias, agent_id, '["skill-worker"]', "[]", env_id, env_rev,
         "[]", "{}", mp_id, mp_rev, binding_rev, enabled, 1, revision, "{}", TS, TS),
    )


def _insert_binding(conn, agent_id, env_id="env-1", binding_rev=1, env_rev=1,
                    host_agent_id="host-1", execution_profile_ref=None):
    conn.execute(
        "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id, environment_revision,"
        " adapter_type, connector_config_ref, execution_profile_ref, host_agent_id, config_digest,"
        " created_by_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, binding_rev, env_id, env_rev, "workbuddy", "conn-1", execution_profile_ref,
         host_agent_id, DIGEST_A, "{}", TS),
    )


def _seed_cycle(conn, agent_id="dev-1", env_id="env-1", mp_id="mp-1", binding_rev=1,
                normalized_alias=None, enabled=1, revision=1):
    """同一事务插 member + binding（循环 FK，DEFERRABLE INITIALLY DEFERRED 提交时校验）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _insert_member(conn, agent_id, normalized_alias=normalized_alias, env_id=env_id, mp_id=mp_id,
                       binding_rev=binding_rev, enabled=enabled, revision=revision)
        _insert_binding(conn, agent_id, env_id=env_id, binding_rev=binding_rev)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _insert_check(conn, check_id="chk-1", agent_id="dev-1", binding_rev=1, env_id="env-1", env_rev=1,
                  mp_id=None, mp_rev=None):
    conn.execute(
        "INSERT INTO agent_checks (check_id, agent_id, binding_revision, environment_id, environment_revision,"
        " config_digest, model_profile_id, model_profile_revision, check_type, status, evidence_level,"
        " observed_at, expires_at, evidence_ref, failure_reason, requested_json, observed_json, created_by_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (check_id, agent_id, binding_rev, env_id, env_rev, DIGEST_A, mp_id, mp_rev, "config", "passed",
         "SYNTHETIC", TS, TS, "evidence://x", None, "{}", None, "{}"),
    )


def _insert_attempt_binding(conn, attempt_id="att-legacy-1", agent_id="dev-1", binding_rev=1,
                            env_id="env-1", env_rev=1, mp_id="mp-1", mp_rev=1):
    conn.execute(
        "INSERT INTO attempt_execution_bindings (attempt_id, agent_id, binding_revision, environment_id,"
        " environment_revision, model_profile_id, model_profile_revision, source_card_digest, workspace,"
        " permissions_json, check_ids_json, resolved_config_digest, requested_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (attempt_id, agent_id, binding_rev, env_id, env_rev, mp_id, mp_rev, DIGEST_A, "/tmp/ws",
         "{}", "[]", DIGEST_A, "{}", TS),
    )


def _insert_change(conn, change_id="chg-1", idempotency_key="idem-1", object_id="dev-1"):
    conn.execute(
        "INSERT INTO registry_changes (change_id, idempotency_key, content_digest, action, object_type,"
        " object_id, before_revision, after_revision, caller_json, result_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (change_id, idempotency_key, DIGEST_A, "create", "agent", object_id, None, 1, "{}", "{}", TS),
    )


def _insert_project_agent(conn, project_id="proj-1", agent_id="dev-1"):
    conn.execute(
        "INSERT INTO project_agents (project_id, agent_id, project_roles_json, permission_ceiling_json,"
        " enabled, revision, created_by_json, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, agent_id, '["skill-worker"]', "{}", 1, 1, "{}", TS, TS),
    )


def _deny_pragma(name):
    """authorizer：拒绝指定 PRAGMA（其余放行）。"""

    def authorizer(action, arg1, arg2, db, source):
        if action == sqlite3.SQLITE_PRAGMA and arg1 == name:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer


def _deny_read(table):
    """authorizer：拒绝读取指定表（其余放行）。"""

    def authorizer(action, arg1, arg2, db, source):
        if action == sqlite3.SQLITE_READ and arg1 == table:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer


class RegistryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.path = Path(self.tmp.name) / "legacy.sqlite3"
        self.store = _make_legacy(self.path)
        self.conn = self.store.conn

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _apply(self, **over):
        kwargs = {"registry_id": REGISTRY_ID, "source_digest": DIGEST_A}
        kwargs.update(over)
        return apply_registry_candidate_migration(self.conn, **kwargs)

    def _managed_names_present(self):
        return _names(self.conn) & _MANAGED

    # -- 迁移成功 / 幂等 -----------------------------------------------------
    def test_apply_creates_nine_tables_and_twelve_triggers(self):
        result = self._apply()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["schemaVersion"], 1)
        self.assertEqual(result["authority"], "candidate")
        self.assertEqual(result["registryId"], REGISTRY_ID)
        self.assertFalse(result["replayed"])
        present = _names(self.conn)
        for name in rm._REGISTRY_TABLE_NAMES:
            self.assertIn(name, present)
        self.assertEqual(set(rm._REGISTRY_TRIGGER_NAMES) - present, set())

    def test_apply_leaves_non_metadata_tables_empty_and_no_seed(self):
        self._apply()
        for name in rm._REGISTRY_TABLE_NAMES:
            if name == "registry_metadata":
                continue
            count = self.conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
            self.assertEqual(count, 0, name)
        # 旧 agents 仍四列；旧 seed 未生成 registry_agents。
        cols = [r[1] for r in self.conn.execute("PRAGMA table_info(agents)")]
        self.assertEqual(len(cols), 4)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM registry_agents").fetchone()[0], 0)

    def test_apply_preserves_legacy_schema_and_rows(self):
        before_master = _dump_old_master(self.conn)
        before_rows = _dump_legacy(self.conn)
        self._apply()
        self.assertEqual(_dump_old_master(self.conn), before_master)
        self.assertEqual(_dump_legacy(self.conn), before_rows)

    def test_inspect_unmigrated_on_legacy_db(self):
        res = inspect_registry_schema(self.conn)
        self.assertEqual(res["status"], "unmigrated")
        self.assertEqual(res["schemaVersion"], 0)
        self.assertIsNone(res["registryId"])
        self.assertIsNone(res["authority"])
        self.assertEqual(self._managed_names_present(), set())

    def test_inspect_ready_after_apply(self):
        self._apply()
        res = inspect_registry_schema(self.conn)
        self.assertEqual(res["status"], "ready")
        self.assertEqual(res["schemaVersion"], 1)
        self.assertEqual(res["registryId"], REGISTRY_ID)
        self.assertEqual(res["authority"], "candidate")
        self.assertEqual(res["sourceDigest"], DIGEST_A)
        self.assertEqual(res["revision"], 1)
        self.assertTrue(res["createdAt"])

    def test_apply_is_idempotent_replay(self):
        first = self._apply()
        row1 = self.conn.execute("SELECT * FROM registry_metadata").fetchone()
        second = self._apply()
        row2 = self.conn.execute("SELECT * FROM registry_metadata").fetchone()
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(tuple(row1), tuple(row2))
        self.assertEqual(second["createdAt"], first["createdAt"])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM registry_metadata").fetchone()[0], 1)

    def test_apply_rejects_different_source_digest(self):
        self._apply(source_digest=DIGEST_A)
        with self.assertRaises(ContractError) as cm:
            self._apply(source_digest=DIGEST_B)
        self.assertEqual(cm.exception.code, "invalid_task")
        row = self.conn.execute("SELECT source_digest FROM registry_metadata").fetchone()
        self.assertEqual(row[0], DIGEST_A)

    def test_apply_rejects_different_registry_id(self):
        self._apply(registry_id="reg-1")
        with self.assertRaises(ContractError):
            self._apply(registry_id="reg-2")
        self.assertEqual(
            self.conn.execute("SELECT registry_id FROM registry_metadata").fetchone()[0], "reg-1"
        )

    # -- inspect 拒绝路径 ---------------------------------------------------
    def test_inspect_with_matching_registry_id_ready(self):
        self._apply()
        res = inspect_registry_schema(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(res["status"], "ready")

    def test_inspect_rejects_registry_id_mismatch(self):
        self._apply()
        with self.assertRaises(ContractError):
            inspect_registry_schema(self.conn, registry_id="reg-zzz")

    def test_inspect_rejects_invalid_registry_id_param(self):
        with self.assertRaises(ContractError):
            inspect_registry_schema(self.conn, registry_id="-bad")

    def test_inspect_rejects_objects_without_metadata(self):
        self.conn.execute("CREATE TABLE registry_changes (change_id TEXT)")
        with self.assertRaises(ContractError):
            inspect_registry_schema(self.conn)

    def test_inspect_rejects_empty_metadata_table(self):
        self.conn.execute(
            "CREATE TABLE registry_metadata (singleton INTEGER, registry_id TEXT, schema_version INTEGER,"
            " authority TEXT, revision INTEGER, source_digest TEXT, created_at TEXT)"
        )
        with self.assertRaises(ContractError):
            inspect_registry_schema(self.conn)

    def test_inspect_rejects_unknown_high_version(self):
        self.conn.execute(
            "CREATE TABLE registry_metadata (singleton INTEGER, registry_id TEXT, schema_version INTEGER,"
            " authority TEXT, revision INTEGER, source_digest TEXT, created_at TEXT)"
        )
        self.conn.execute(
            "INSERT INTO registry_metadata VALUES (1,'reg-x',2,'candidate',1,?,?)", (DIGEST_A, TS)
        )
        with self.assertRaises(ContractError):
            inspect_registry_schema(self.conn)
        with self.assertRaises(ContractError):
            apply_registry_candidate_migration(self.conn, registry_id=REGISTRY_ID, source_digest=DIGEST_A)

    def test_inspect_rejects_relaxed_table_definition(self):
        self._apply()
        self.conn.execute("DROP TABLE registry_changes")
        self.conn.execute("CREATE TABLE registry_changes (change_id TEXT)")
        with self.assertRaises(ContractError):
            inspect_registry_schema(self.conn)

    def test_inspect_rejects_empty_trigger(self):
        self._apply()
        self.conn.execute("DROP TRIGGER registry_environments_no_update")
        self.conn.execute(
            "CREATE TRIGGER registry_environments_no_update BEFORE UPDATE ON environments BEGIN SELECT 1; END"
        )
        with self.assertRaises(ContractError):
            inspect_registry_schema(self.conn)

    # -- apply 输入校验 -----------------------------------------------------
    def test_apply_rejects_bad_registry_id(self):
        with self.assertRaises(ContractError):
            apply_registry_candidate_migration(self.conn, registry_id="-bad", source_digest=DIGEST_A)
        self.assertEqual(self._managed_names_present(), set())

    def test_apply_rejects_digest_with_trailing_newline(self):
        with self.assertRaises(ContractError):
            apply_registry_candidate_migration(self.conn, registry_id=REGISTRY_ID, source_digest=DIGEST_A + "\n")
        self.assertEqual(self._managed_names_present(), set())

    def test_apply_rejects_foreign_keys_off(self):
        conn = sqlite3.connect(str(self.path), isolation_level=None)
        try:
            self.assertEqual(conn.execute("PRAGMA foreign_keys").fetchone()[0], 0)
            with self.assertRaises(ContractError):
                apply_registry_candidate_migration(conn, registry_id=REGISTRY_ID, source_digest=DIGEST_A)
        finally:
            conn.close()
        self.assertEqual(self._managed_names_present(), set())

    def test_apply_rejects_caller_transaction(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            with self.assertRaises(ContractError):
                self._apply()
            self.assertTrue(self.conn.in_transaction)
        finally:
            self.conn.execute("ROLLBACK")
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self._managed_names_present(), set())

    # -- 回滚 ---------------------------------------------------------------
    def test_apply_rolls_back_on_ddl_failure(self):
        before_rows = _dump_legacy(self.conn)
        original = rm._migration_statements

        def boom():
            statements = list(original())
            statements.append(("__bad__", "CREATE TABLE __bad__ ("))
            return statements

        with mock.patch.object(rm, "_migration_statements", boom):
            with self.assertRaises(sqlite3.Error):
                self._apply()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self._managed_names_present(), set())
        self.assertEqual(_dump_legacy(self.conn), before_rows)

    def test_apply_rolls_back_on_metadata_failure(self):
        def boom(conn, registry_id, source_digest, created_at):
            raise RuntimeError("metadata write failed")

        with mock.patch.object(rm, "_write_metadata", boom):
            with self.assertRaises(RuntimeError):
                self._apply()
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(self._managed_names_present(), set())

    # -- 约束 / 历史保护 ----------------------------------------------------
    def _seed_all_immutable_rows(self):
        self._apply()
        _insert_env(self.conn)
        _insert_model(self.conn)
        _seed_cycle(self.conn, "dev-1")
        _insert_check(self.conn)
        _insert_attempt_binding(self.conn)
        _insert_change(self.conn)

    def test_deferred_cycle_member_binding_commit(self):
        self._apply()
        self.conn.execute("INSERT INTO agents (agent_id, label) VALUES ('dev-cycle-1','循环成员')")
        _insert_env(self.conn, env_id="env-c")
        _insert_model(self.conn, mp_id="mp-c", env_id="env-c", strategy="host-default",
                      provider=None, model_id=None)
        _seed_cycle(self.conn, "dev-cycle-1", env_id="env-c", mp_id="mp-c")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        count = self.conn.execute(
            "SELECT COUNT(*) FROM registry_agents WHERE agent_id='dev-cycle-1'"
        ).fetchone()[0]
        self.assertEqual(count, 1)

    def test_constraints_reject_bad_rows(self):
        self._apply()
        _insert_env(self.conn)
        _insert_model(self.conn)
        _seed_cycle(self.conn, "dev-1")

        # 缺 FK：agent_id 不在旧 agents
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_member(self.conn, "ghost-agent")
        # 缺 FK：model 环境不存在
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_model(self.conn, mp_id="mp-2", env_id="ghost-env")
        # 别名冲突：normalized_alias 与 dev-1 重复
        self.conn.execute("INSERT INTO agents (agent_id, label) VALUES ('dev-2','别名冲突成员')")
        with self.assertRaises(sqlite3.IntegrityError):
            _seed_cycle(self.conn, "dev-2", normalized_alias="dev-1")
        # 布尔位非法：enabled=2
        self.conn.execute("INSERT INTO agents (agent_id, label) VALUES ('dev-3','布尔成员')")
        with self.assertRaises(sqlite3.IntegrityError):
            _seed_cycle(self.conn, "dev-3", enabled=2)
        # 0 revision
        self.conn.execute("INSERT INTO agents (agent_id, label) VALUES ('dev-4','零版本成员')")
        with self.assertRaises(sqlite3.IntegrityError):
            _seed_cycle(self.conn, "dev-4", revision=0)
        # 坏 strategy
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_model(self.conn, mp_id="mp-bad", strategy="bogus", provider=None, model_id=None)
        # fixed 缺 provider
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_model(self.conn, mp_id="mp-bad2", strategy="fixed", provider=None, model_id="m")
        # host-default 带 provider
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_model(self.conn, mp_id="mp-bad3", strategy="host-default", provider="p", model_id=None)
        # binding profile/host 双 null
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_binding(self.conn, "dev-1", binding_rev=2, host_agent_id=None, execution_profile_ref=None)
        # binding 0 revision
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_binding(self.conn, "dev-1", binding_rev=0, host_agent_id="h")

    def test_immutable_tables_reject_update_and_delete(self):
        self._seed_all_immutable_rows()
        cases = (
            ("environments", "environment_id='env-1' AND revision=1", "host_type"),
            ("model_profiles", "model_profile_id='mp-1' AND profile_revision=1", "strategy"),
            ("agent_bindings", "agent_id='dev-1' AND binding_revision=1", "adapter_type"),
            ("agent_checks", "check_id='chk-1'", "status"),
            ("attempt_execution_bindings", "attempt_id='att-legacy-1'", "workspace"),
            ("registry_changes", "change_id='chg-1'", "action"),
        )
        for table, where, column in cases:
            before = tuple(self.conn.execute("SELECT * FROM %s WHERE %s" % (table, where)).fetchone())
            with self.assertRaises(sqlite3.IntegrityError) as cm:
                self.conn.execute(
                    "UPDATE %s SET %s = %s WHERE %s" % (table, column, column, where)
                )
            self.assertIn("registry_history_immutable", str(cm.exception))
            with self.assertRaises(sqlite3.IntegrityError):
                self.conn.execute("DELETE FROM %s WHERE %s" % (table, where))
            after = tuple(self.conn.execute("SELECT * FROM %s WHERE %s" % (table, where)).fetchone())
            self.assertEqual(before, after, table)

    def test_foreign_key_check_empty_after_apply(self):
        self._apply()
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    # -- 并发 ---------------------------------------------------------------
    def test_concurrent_apply_two_connections(self):
        results = {}

        def worker(index):
            conn = _open(self.path)
            try:
                results[index] = apply_registry_candidate_migration(
                    conn, registry_id=REGISTRY_ID, source_digest=DIGEST_A
                )
            except Exception as exc:  # pragma: no cover - 仅失败时记录
                results[index] = exc
            finally:
                conn.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in (0, 1)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        for index in (0, 1):
            self.assertIsInstance(results[index], dict, "worker %d failed: %r" % (index, results[index]))
        self.assertEqual(sorted(r["replayed"] for r in results.values()), [False, True])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM registry_metadata").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    # -- 只读 ---------------------------------------------------------------
    def test_read_only_store_does_not_migrate(self):
        store2 = SqliteStore(self.path, read_only=True)
        try:
            res = inspect_registry_schema(store2.conn)
            self.assertEqual(res["status"], "unmigrated")
            present = {r[0] for r in store2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            ).fetchall()}
            self.assertEqual(present & _MANAGED, set())
        finally:
            store2.close()

    def test_read_only_apply_fails(self):
        store2 = SqliteStore(self.path, read_only=True)
        try:
            with self.assertRaises((sqlite3.DatabaseError, ContractError)):
                apply_registry_candidate_migration(
                    store2.conn, registry_id=REGISTRY_ID, source_digest=DIGEST_A
                )
            present = {r[0] for r in store2.conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','trigger')"
            ).fetchall()}
            self.assertEqual(present & _MANAGED, set())
        finally:
            store2.close()

    def test_missing_path_read_only_store_rejected(self):
        missing = Path(self.tmp.name) / "does-not-exist.sqlite3"
        with self.assertRaises(sqlite3.OperationalError):
            SqliteStore(missing, read_only=True)

    # =====================================================================
    # 02A2-R1 三项边界返工：新增负例与正例（不改上面原有 30 个测试）
    # =====================================================================
    def _all_master_names(self):
        return {r[0] for r in self.conn.execute("SELECT name FROM sqlite_master").fetchall()}

    def _seed_valid_fixture(self):
        """合法合成 fixture：env/model/循环 member+binding/check/attempt binding/change/project agent。

        复用现有 insert helper，保证除“被测空 ID 列”外每一列都合法（含合法循环成员关系）。
        """
        self._apply()
        _insert_env(self.conn)
        _insert_model(self.conn)
        _seed_cycle(self.conn, "dev-1")
        _insert_check(self.conn)
        _insert_attempt_binding(self.conn)
        _insert_change(self.conn)
        _insert_project_agent(self.conn)

    # -- 1) 空 ID 约束：逐列参数化直接 SQL 负例 ----------------------------
    def test_empty_required_id_rejected_direct_sql(self):
        """每个 required ID 列写入空串必须被 CHECK 拒绝（合法合成循环 fixture）。"""
        self._seed_valid_fixture()
        cases = (
            ("environments.environment_id", lambda: _insert_env(self.conn, env_id="", rev=2)),
            ("environments.instance_id",
             lambda: _insert_env(self.conn, env_id="env-z", rev=1, instance_id="")),
            ("model_profiles.model_profile_id", lambda: _insert_model(self.conn, mp_id="")),
            ("model_profiles.environment_id",
             lambda: _insert_model(self.conn, mp_id="mp-z", env_id="")),
            ("registry_agents.agent_id", lambda: _insert_member(self.conn, "")),
            ("registry_agents.environment_id",
             lambda: _insert_member(self.conn, "dev-e", env_id="")),
            ("registry_agents.model_profile_id",
             lambda: _insert_member(self.conn, "dev-m", mp_id="")),
            ("agent_bindings.agent_id", lambda: _insert_binding(self.conn, "", binding_rev=5)),
            ("agent_bindings.environment_id",
             lambda: _insert_binding(self.conn, "dev-1", binding_rev=6, env_id="")),
            ("project_agents.project_id",
             lambda: _insert_project_agent(self.conn, project_id="")),
            ("project_agents.agent_id",
             lambda: _insert_project_agent(self.conn, project_id="proj-z", agent_id="")),
            ("agent_checks.check_id", lambda: _insert_check(self.conn, check_id="")),
            ("agent_checks.agent_id",
             lambda: _insert_check(self.conn, check_id="chk-a", agent_id="")),
            ("agent_checks.environment_id",
             lambda: _insert_check(self.conn, check_id="chk-b", env_id="")),
            ("attempt_execution_bindings.attempt_id",
             lambda: _insert_attempt_binding(self.conn, attempt_id="")),
            ("attempt_execution_bindings.agent_id",
             lambda: _insert_attempt_binding(self.conn, attempt_id="att-a", agent_id="")),
            ("attempt_execution_bindings.environment_id",
             lambda: _insert_attempt_binding(self.conn, attempt_id="att-b", env_id="")),
            ("attempt_execution_bindings.model_profile_id",
             lambda: _insert_attempt_binding(self.conn, attempt_id="att-c", mp_id="")),
            ("registry_changes.change_id",
             lambda: _insert_change(self.conn, change_id="", idempotency_key="idem-a")),
            ("registry_changes.object_id",
             lambda: _insert_change(self.conn, change_id="chg-z", idempotency_key="idem-b", object_id="")),
        )
        for label, call in cases:
            with self.subTest(case=label):
                with self.assertRaises(sqlite3.IntegrityError):
                    call()

    def test_empty_registry_id_rejected_direct_sql(self):
        """registry_metadata.registry_id 空串必须被 CHECK 拒绝（清空 singleton 行后直插）。"""
        self._apply()
        self.conn.execute("DELETE FROM registry_metadata")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "INSERT INTO registry_metadata (singleton, registry_id, schema_version, authority,"
                " revision, source_digest, created_at) VALUES (1,'',1,'candidate',1,?,?)",
                (DIGEST_A, TS),
            )

    def test_empty_nullable_id_rejected_when_present(self):
        """nullable ID 非 NULL 时不得为空串（host_agent_id / agent_checks.model_profile_id）。"""
        self._seed_valid_fixture()
        with self.assertRaises(sqlite3.IntegrityError):
            # execution_profile_ref 非空使既有绑定 CHECK 通过，专门触发新的 nullable 空串约束
            _insert_binding(self.conn, "dev-1", binding_rev=7, host_agent_id="",
                            execution_profile_ref="conn-x")
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_check(self.conn, check_id="chk-c", mp_id="", mp_rev=1)

    def test_empty_model_id_rejected_for_fixed_profile(self):
        """fixed 模型 model_id 空串拒绝（既有策略 CHECK + ID 非空）。"""
        self._seed_valid_fixture()
        with self.assertRaises(sqlite3.IntegrityError):
            _insert_model(self.conn, mp_id="mp-fe", model_id="")

    def test_valid_fixture_accepts_all_id_columns(self):
        """正例：合法合成循环 fixture 全部 ID 非空，迁移 ready 且 FK 自检干净。"""
        self._seed_valid_fixture()
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(inspect_registry_schema(self.conn)["status"], "ready")

    def test_nullable_id_columns_accept_null(self):
        """正例：host-default 模型 model_id=NULL、可选 model 检查 NULL、host_agent_id=NULL 均允许。"""
        self._apply()
        _insert_env(self.conn)
        _insert_model(self.conn, mp_id="mp-hd", strategy="host-default", provider=None, model_id=None)
        self.conn.execute("INSERT INTO agents (agent_id, label) VALUES ('dev-null','可空成员')")
        _seed_cycle(self.conn, "dev-null", mp_id="mp-hd")
        _insert_check(self.conn, check_id="chk-null", agent_id="dev-null")
        _insert_binding(self.conn, "dev-null", binding_rev=2, host_agent_id=None,
                        execution_profile_ref="conn-x")
        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

    # -- 2) SQLite 运行异常传播 / 授权拒绝 ---------------------------------
    def test_apply_pragma_denied_preserves_sqlite_error(self):
        """authorizer 拒绝 PRAGMA foreign_keys：保留 sqlite3 异常，不改成配置类拒绝且无库改变。"""
        conn = _open(self.path)
        conn.set_authorizer(_deny_pragma("foreign_keys"))
        try:
            with self.assertRaises(sqlite3.DatabaseError) as cm:
                apply_registry_candidate_migration(conn, registry_id=REGISTRY_ID, source_digest=DIGEST_A)
            self.assertNotIsInstance(cm.exception, ContractError)
            self.assertFalse(conn.in_transaction)
        finally:
            conn.close()
        self.assertEqual(self._all_master_names() & _MANAGED, set())

    def test_inspect_metadata_read_denied_preserves_sqlite_error(self):
        """authorizer 拒绝读 registry_metadata：只读 inspect 保留 sqlite3 异常且不写库。"""
        self._apply()
        before = tuple(self.conn.execute("SELECT * FROM registry_metadata").fetchone())
        self.conn.set_authorizer(_deny_read("registry_metadata"))
        try:
            with self.assertRaises(sqlite3.DatabaseError) as cm:
                inspect_registry_schema(self.conn)
            self.assertNotIsInstance(cm.exception, ContractError)
        finally:
            # 本机 SQLite 3.51.0 上 set_authorizer(None) 不足以恢复读权限（读回仍 not authorized），
            # 因此显式安装一个总返回 SQLITE_OK 的 callback 恢复授权，不依赖 None 语义。
            self.conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)
        after = tuple(self.conn.execute("SELECT * FROM registry_metadata").fetchone())
        self.assertEqual(before, after)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM registry_metadata").fetchone()[0], 1)
        self.assertEqual(inspect_registry_schema(self.conn)["status"], "ready")

    def test_apply_rejects_managed_view_collision_and_rolls_back(self):
        """apply 遇同名 view 冲突：受控拒绝、本函数事务收口、view 不被接管/覆盖。"""
        self.conn.execute("CREATE VIEW registry_metadata AS SELECT 1 AS singleton")
        with self.assertRaises(ContractError) as cm:
            self._apply()
        self.assertEqual(cm.exception.code, "invalid_task")
        self.assertFalse(self.conn.in_transaction)
        row = self.conn.execute(
            "SELECT type FROM sqlite_master WHERE name = 'registry_metadata'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "view")

    # -- 3) managed 同名对象全类型扫描（table/view/index/trigger） ----------
    def test_inspect_rejects_managed_view_collision(self):
        self.conn.execute("CREATE VIEW registry_metadata AS SELECT 1 AS singleton")
        with self.assertRaises(ContractError) as cm:
            inspect_registry_schema(self.conn)
        self.assertEqual(cm.exception.code, "invalid_task")
        self.assertEqual(cm.exception.details.get("reason"), "object_type_mismatch")

    def test_inspect_rejects_managed_index_collision(self):
        self.conn.execute("CREATE INDEX registry_metadata ON tasks (task_id)")
        with self.assertRaises(ContractError) as cm:
            inspect_registry_schema(self.conn)
        self.assertEqual(cm.exception.code, "invalid_task")
        self.assertEqual(cm.exception.details.get("reason"), "object_type_mismatch")

    def test_inspect_rejects_managed_object_without_metadata(self):
        self.conn.execute("CREATE VIEW environments AS SELECT 1 AS environment_id")
        with self.assertRaises(ContractError) as cm:
            inspect_registry_schema(self.conn)
        self.assertEqual(cm.exception.code, "invalid_task")
        self.assertEqual(cm.exception.details.get("field"), "registry_metadata")
        self.assertEqual(cm.exception.details.get("reason"), "incomplete_registry")

    def test_inspect_unmigrated_with_unrelated_view_and_index(self):
        """正例：非 managed 的 view/index 不影响普通旧库，仍判 unmigrated。"""
        self.conn.execute("CREATE VIEW legacy_view AS SELECT 1 AS x")
        self.conn.execute("CREATE INDEX idx_legacy_extra ON tasks (task_id)")
        res = inspect_registry_schema(self.conn)
        self.assertEqual(res["status"], "unmigrated")


if __name__ == "__main__":
    unittest.main()
