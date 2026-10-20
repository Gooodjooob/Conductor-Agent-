"""D4-NEXT-02B1A 定向测试：候选成员列表只读服务（接口合同 §13.10）。

运行（cox 独立运行，工作目录 shared/agent-orchestrator）::

    python3 -B -m unittest discover -s tests -p test_d4_registry_read.py
    python3 -B -m unittest discover -s tests -p 'test_d4*.py'

范围：
- 只新增本测试文件与 service/registry_read.py；不改旧 Python / __init__ / CLI /
  adapter / schema / 合同 / 计划 / 名册 / DB / 配置。
- 只测本卡新生成的 SYNTHETIC candidate 库（SqliteStore + apply 迁移器生成），
  不使用 development.sqlite / 历史 fixture / 正式库。
- worker 侧 Bash 零尝试、测试 NOT_RUN；由 cox 在既有权限内独立补验。
- 每个用例写本轮隔离 runtime（TemporaryDirectory 沿 TMPDIR，可用
  D4_REGISTRY_READ_RUNTIME_DIR 覆盖），不写历史 fixture 默认 root。
"""
import json
import math
import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import storage.registry_migrations as rm
from contracts.schemas import ContractError
from service.registry_read import list_registry_agents
from storage.registry_migrations import apply_registry_candidate_migration
from storage.sqlite_store import SqliteStore

TS = "2026-10-08T00:00:00+08:00"
TS_LATER = "2026-10-08T01:00:00+08:00"
TS_NEWEST = "2026-10-08T02:00:00+08:00"
TS_OLD = "2026-10-07T23:00:00+08:00"
TS_EXPIRED = "2020-01-01T00:00:00+08:00"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64

REGISTRY_ID = "reg-1"
SOURCE_DIGEST = "sha256:" + "0" * 64

ROOT = "/Users/demo/.hermes/skills/conductor"
ROOTS_JSON = json.dumps([ROOT])
CEILING_JSON = json.dumps(
    {
        "writeScope": [ROOT + "/shared/agent-orchestrator"],
        "allowNetwork": False,
        "allowBusinessWrite": False,
        "allowGit": False,
        "allowPublish": False,
    }
)

LEGACY_TABLES = ("sources", "agents", "tasks", "attempts", "events", "deliveries", "reviews")
REGISTRY_TABLES = rm._REGISTRY_TABLE_NAMES

ITEM_KEYS = [
    "agentId", "agentAlias", "normalizedAlias", "displayName", "roles", "capabilities",
    "enabled", "registrationComplete", "revision", "allowedRoots", "permissionCeiling",
    "createdAt", "updatedAt", "environment", "binding", "modelProfile", "projects", "checks",
]
ENVIRONMENT_KEYS = [
    "environmentId", "revision", "hostType", "instanceId", "hostVersion", "adapterType",
    "connectorConfigRef", "configDigest", "enabled",
]
BINDING_KEYS = [
    "agentId", "bindingRevision", "environmentId", "environmentRevision", "adapterType",
    "connectorConfigRef", "executionProfileRef", "hostAgentId", "configDigest",
]
MODEL_KEYS = [
    "modelProfileId", "profileRevision", "environmentId", "environmentRevision", "strategy",
    "provider", "modelId", "configDigest",
]
PROJECT_KEYS = ["projectId", "roles", "permissionCeiling", "enabled", "revision", "createdAt", "updatedAt"]
CHECK_KEYS = [
    "checkId", "agentId", "bindingRevision", "environmentId", "environmentRevision",
    "configDigest", "modelProfileId", "modelProfileRevision", "checkType", "status",
    "evidenceLevel", "observedAt", "expiresAt", "evidenceRef", "failureReason",
    "requested", "observed",
]


def runtime_root():
    """本轮隔离 runtime；默认沿 TMPDIR，可用 D4_REGISTRY_READ_RUNTIME_DIR 覆盖。"""
    base = os.environ.get("D4_REGISTRY_READ_RUNTIME_DIR")
    root = Path(base) if base else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


# -- fixture helper ---------------------------------------------------------
def _make_legacy(path):
    """用现有 SqliteStore 生成 legacy schema + 合成旧行（三个旧 agent，供 FK 引用）。"""
    store = SqliteStore(path)
    conn = store.conn
    conn.execute("INSERT INTO sources (source_id, label, registered_at) VALUES ('src-1','本地来源',?)", (TS,))
    for agent_id, label in (("dev-wkb1", "成员A"), ("cox-arch", "成员B"), ("solo-1", "成员C")):
        conn.execute(
            "INSERT INTO agents (agent_id, label, note, registered_at) VALUES (?,?,?,?)",
            (agent_id, label, "synthetic registry entry", TS),
        )
    conn.execute(
        "INSERT INTO tasks (task_id, project_id, title, status, revision, created_at, updated_at)"
        " VALUES ('task-legacy-1','d4-product','旧任务','closed',1,?,?)", (TS, TS)
    )
    conn.execute(
        "INSERT INTO attempts (attempt_id, task_id, ordinal, state, created_at)"
        " VALUES ('att-legacy-1','task-legacy-1',1,'delivered',?)", (TS,)
    )
    return store


def _insert_env(conn, env_id="env-1", rev=1, host_type="workbuddy", instance_id="inst-1",
                host_version="1.4.0", adapter_type="workbuddy", connector="conn-1",
                digest=DIGEST_A, enabled=1):
    conn.execute(
        "INSERT INTO environments (environment_id, revision, host_type, instance_id, host_version,"
        " adapter_type, connector_config_ref, config_digest, allowed_roots_json, permission_ceiling_json,"
        " enabled, created_by_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (env_id, rev, host_type, instance_id, host_version, adapter_type, connector, digest,
         ROOTS_JSON, CEILING_JSON, enabled, "{}", TS),
    )


def _insert_model(conn, mp_id="mp-1", mp_rev=1, env_id="env-1", env_rev=1, strategy="fixed",
                  provider="synthetic-provider", model_id="synthetic-model", digest=DIGEST_A):
    conn.execute(
        "INSERT INTO model_profiles (model_profile_id, profile_revision, environment_id,"
        " environment_revision, strategy, provider, model_id, config_digest, created_by_json,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (mp_id, mp_rev, env_id, env_rev, strategy, provider, model_id, digest, "{}", TS),
    )


def _insert_member(conn, agent_id, alias=None, display=None, roles='["skill-worker"]',
                   capabilities='["develop"]', env_id="env-1", env_rev=1, mp_id="mp-1",
                   mp_rev=1, binding_rev=1, enabled=1, revision=1, normalized=None,
                   roots=ROOTS_JSON, ceiling=CEILING_JSON):
    conn.execute(
        "INSERT INTO registry_agents (agent_id, agent_alias, normalized_alias, display_name,"
        " roles_json, capabilities_json, environment_id, environment_revision, allowed_roots_json,"
        " permission_ceiling_json, model_profile_id, model_profile_revision, current_binding_revision,"
        " enabled, registration_complete, revision, source_row_json, created_by_json, created_at,"
        " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, alias or agent_id, normalized or agent_id, display or agent_id, roles,
         capabilities, env_id, env_rev, roots, ceiling, mp_id, mp_rev, binding_rev, enabled,
         1, revision, None, "{}", TS, TS),
    )


def _insert_binding(conn, agent_id, binding_rev=1, env_id="env-1", env_rev=1,
                    adapter_type="workbuddy", connector="conn-1", execution_profile_ref=None,
                    host_agent_id="host-wkb1", digest=DIGEST_A):
    conn.execute(
        "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id, environment_revision,"
        " adapter_type, connector_config_ref, execution_profile_ref, host_agent_id, config_digest,"
        " created_by_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, binding_rev, env_id, env_rev, adapter_type, connector, execution_profile_ref,
         host_agent_id, digest, "{}", TS),
    )


def _seed_cycle(conn, member_kwargs, binding_kwargs):
    """同一事务插 member + binding（循环 FK，DEFERRABLE INITIALLY DEFERRED 提交时校验）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _insert_member(conn, **member_kwargs)
        _insert_binding(conn, **binding_kwargs)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _insert_project_agent(conn, project_id="proj-a", agent_id="dev-wkb1",
                          roles='["skill-worker"]', enabled=1, revision=1):
    conn.execute(
        "INSERT INTO project_agents (project_id, agent_id, project_roles_json,"
        " permission_ceiling_json, enabled, revision, created_by_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, agent_id, roles, CEILING_JSON, enabled, revision, "{}", TS, TS),
    )


def _insert_check(conn, check_id="chk-1", agent_id="dev-wkb1", binding_rev=1, env_id="env-1",
                  env_rev=1, digest=DIGEST_A, mp_id=None, mp_rev=None, check_type="config",
                  status="passed", evidence_level="SYNTHETIC", observed_at=TS, expires_at=TS_LATER,
                  evidence_ref="evidence://synthetic/1", failure_reason=None,
                  requested='{"synthetic": true}', observed=None):
    conn.execute(
        "INSERT INTO agent_checks (check_id, agent_id, binding_revision, environment_id,"
        " environment_revision, config_digest, model_profile_id, model_profile_revision,"
        " check_type, status, evidence_level, observed_at, expires_at, evidence_ref,"
        " failure_reason, requested_json, observed_json, created_by_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (check_id, agent_id, binding_rev, env_id, env_rev, digest, mp_id, mp_rev, check_type,
         status, evidence_level, observed_at, expires_at, evidence_ref, failure_reason,
         requested, observed, "{}"),
    )


def _seed_two_members(conn):
    """两个成员：dev-wkb1（workbuddy / fixed / enabled）与 cox-arch（codex / host-default / 未启用）。"""
    _insert_env(conn, env_id="env-1", rev=1, host_type="workbuddy", instance_id="inst-wkb-1",
                host_version="1.4.0", adapter_type="workbuddy", connector="conn-1", digest=DIGEST_A)
    _insert_env(conn, env_id="env-2", rev=1, host_type="codex", instance_id="inst-cx-1",
                host_version=None, adapter_type="codex", connector="conn-2", digest=DIGEST_B)
    _insert_model(conn, mp_id="mp-1", mp_rev=1, env_id="env-1", env_rev=1, strategy="fixed",
                  provider="synthetic-provider", model_id="synthetic-model", digest=DIGEST_A)
    _insert_model(conn, mp_id="mp-2", mp_rev=1, env_id="env-2", env_rev=1,
                  strategy="host-default", provider=None, model_id=None, digest=DIGEST_B)

    _seed_cycle(
        conn,
        {"agent_id": "dev-wkb1", "alias": "Dev-WKB1", "display": "成员A", "env_id": "env-1",
         "mp_id": "mp-1", "enabled": 1},
        {"agent_id": "dev-wkb1", "binding_rev": 1, "env_id": "env-1", "env_rev": 1,
         "adapter_type": "workbuddy", "connector": "conn-1", "host_agent_id": "host-wkb1",
         "digest": DIGEST_A},
    )
    _seed_cycle(
        conn,
        {"agent_id": "cox-arch", "alias": "CoX", "normalized": "cox", "display": "成员B",
         "roles": '["sales-architect"]',
         "capabilities": '["review","document"]', "env_id": "env-2", "mp_id": "mp-2",
         "enabled": 0},
        {"agent_id": "cox-arch", "binding_rev": 1, "env_id": "env-2", "env_rev": 1,
         "adapter_type": "codex", "connector": "conn-2", "execution_profile_ref": "profile-cox",
         "host_agent_id": None, "digest": DIGEST_B},
    )

    _insert_project_agent(conn, project_id="proj-a", agent_id="dev-wkb1", roles='["skill-worker"]')
    _insert_project_agent(conn, project_id="proj-b", agent_id="cox-arch", roles='["sales-architect"]')

    _insert_check(conn, check_id="chk-current", agent_id="dev-wkb1", env_id="env-1",
                  check_type="config", status="passed", evidence_level="SYNTHETIC",
                  observed_at=TS_NEWEST, expires_at=TS_LATER,
                  evidence_ref="evidence://synthetic/current", requested='{"synthetic": true}',
                  observed='{"observedBy":"synthetic"}')
    _insert_check(conn, check_id="chk-expired", agent_id="dev-wkb1", env_id="env-1", mp_id="mp-1",
                  mp_rev=1, check_type="model", status="unverified", evidence_level="UNVERIFIED",
                  observed_at=TS, expires_at=TS_EXPIRED,
                  evidence_ref="evidence://local/expired", requested='{}')
    _insert_check(conn, check_id="chk-old", agent_id="dev-wkb1", env_id="env-1",
                  check_type="identity", status="failed", evidence_level="LOCAL",
                  observed_at=TS_OLD, expires_at=TS, evidence_ref="evidence://local/old",
                  failure_reason="宿主身份未确认", requested='{}', observed='{"note":"old"}')
    _insert_check(conn, check_id="chk-cox", agent_id="cox-arch", env_id="env-2", env_rev=1,
                  mp_id="mp-2", mp_rev=1, digest=DIGEST_B, check_type="runtime",
                  status="unverified", evidence_level="OFFICIAL", observed_at=TS,
                  evidence_ref="evidence://official/cox", requested='{}')


def _dump_tables(conn):
    """全部 legacy + registry 表的逐行快照（含 sqlite_master 与 total_changes）。"""
    dump = {"__master__": [tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()]}
    for name in LEGACY_TABLES + REGISTRY_TABLES:
        try:
            dump[name] = [tuple(r) for r in conn.execute("SELECT * FROM %s" % name).fetchall()]
        except sqlite3.Error:
            dump[name] = None
    return dump


def _call(conn, **kwargs):
    """调用并返回 (code, details)；非 ContractError 直接抛 AssertionError。"""
    try:
        list_registry_agents(conn, **kwargs)
    except ContractError as exc:
        return exc.code, exc.details
    raise AssertionError("期望受控 ContractError，实际成功返回")


def _deny_read(table):
    def authorizer(action, arg1, arg2, db, source):
        if action == sqlite3.SQLITE_READ and arg1 == table:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    return authorizer


class RegistryReadTestCase(unittest.TestCase):
    """默认 fixture：legacy 库 + candidate 迁移 + 两个合法合成成员。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.path = Path(self.tmp.name) / "candidate.sqlite3"
        self.store = _make_legacy(self.path)
        self.conn = self.store.conn
        apply_registry_candidate_migration(
            self.conn, registry_id=REGISTRY_ID, source_digest=SOURCE_DIGEST
        )
        _seed_two_members(self.conn)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def items_by_id(self, result):
        return {item["agentId"]: item for item in result["items"]}


class ProjectionTests(RegistryReadTestCase):
    def test_root_keys_and_constants(self):
        result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(
            set(result),
            {"schemaVersion", "registry", "filters", "wouldWrite", "wouldDispatch",
             "authorityUnchanged", "items"},
        )
        self.assertEqual(result["schemaVersion"], "registry-agent-list.v1")
        self.assertFalse(result["wouldWrite"])
        self.assertFalse(result["wouldDispatch"])
        self.assertTrue(result["authorityUnchanged"])
        self.assertEqual(
            set(result["registry"]),
            {"registryId", "schemaVersion", "authority", "revision", "sourceDigest"},
        )
        self.assertEqual(result["registry"]["registryId"], REGISTRY_ID)
        self.assertEqual(result["registry"]["schemaVersion"], 1)
        self.assertEqual(result["registry"]["authority"], "candidate")
        self.assertEqual(result["registry"]["revision"], 1)
        self.assertEqual(result["registry"]["sourceDigest"], SOURCE_DIGEST)
        self.assertEqual(result["filters"], {"host": None, "role": None, "enabled": None})

    def test_item_keys_and_ordering(self):
        result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual([item["agentId"] for item in result["items"]], ["cox-arch", "dev-wkb1"])
        item = self.items_by_id(result)["dev-wkb1"]
        self.assertEqual(list(item), ITEM_KEYS)
        self.assertEqual(list(item["environment"]), ENVIRONMENT_KEYS)
        self.assertEqual(list(item["binding"]), BINDING_KEYS)
        self.assertEqual(list(item["modelProfile"]), MODEL_KEYS)
        self.assertEqual(list(item["projects"][0]), PROJECT_KEYS)
        self.assertEqual(list(item["checks"][0]), CHECK_KEYS)
        # 不从 enabled / passed 提升 online / ready：只读投影没有这类字段。
        for key in ("online", "ready", "status", "verified", "hostVerified"):
            self.assertNotIn(key, item)

    def test_fixed_and_host_default_profiles(self):
        result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        by_id = self.items_by_id(result)
        self.assertEqual(by_id["dev-wkb1"]["modelProfile"]["strategy"], "fixed")
        self.assertEqual(by_id["dev-wkb1"]["modelProfile"]["provider"], "synthetic-provider")
        self.assertEqual(by_id["dev-wkb1"]["modelProfile"]["modelId"], "synthetic-model")
        self.assertEqual(by_id["cox-arch"]["modelProfile"]["strategy"], "host-default")
        self.assertIsNone(by_id["cox-arch"]["modelProfile"]["provider"])
        self.assertIsNone(by_id["cox-arch"]["modelProfile"]["modelId"])
        self.assertTrue(by_id["dev-wkb1"]["enabled"])
        self.assertFalse(by_id["cox-arch"]["enabled"])
        self.assertTrue(by_id["dev-wkb1"]["registrationComplete"])
        self.assertEqual(by_id["cox-arch"]["environment"]["hostType"], "codex")
        self.assertIsNone(by_id["cox-arch"]["environment"]["hostVersion"])

    def test_checks_keep_all_history_and_synthetic(self):
        result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        checks = self.items_by_id(result)["dev-wkb1"]["checks"]
        self.assertEqual([c["checkId"] for c in checks], ["chk-current", "chk-expired", "chk-old"])
        self.assertEqual(checks[0]["status"], "passed")
        # SYNTHETIC + passed 必须仍显示 SYNTHETIC，不升为真。
        self.assertEqual(checks[0]["evidenceLevel"], "SYNTHETIC")
        self.assertEqual(checks[0]["expiresAt"], TS_LATER)
        self.assertEqual(checks[1]["expiresAt"], TS_EXPIRED)
        self.assertEqual(checks[1]["status"], "unverified")
        self.assertEqual(checks[2]["failureReason"], "宿主身份未确认")
        self.assertEqual(checks[0]["requested"], {"synthetic": True})
        self.assertEqual(checks[0]["observed"], {"observedBy": "synthetic"})
        self.assertIsNone(checks[1]["observed"])

    def test_projects_isolated_and_sorted(self):
        result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        by_id = self.items_by_id(result)
        self.assertEqual([p["projectId"] for p in by_id["dev-wkb1"]["projects"]], ["proj-a"])
        self.assertEqual([p["projectId"] for p in by_id["cox-arch"]["projects"]], ["proj-b"])
        self.assertEqual(by_id["dev-wkb1"]["projects"][0]["roles"], ["skill-worker"])
        self.assertEqual(by_id["cox-arch"]["projects"][0]["roles"], ["sales-architect"])

    def test_filters_and_empty_results(self):
        by_host = list_registry_agents(self.conn, registry_id=REGISTRY_ID, host="codex")
        self.assertEqual([i["agentId"] for i in by_host["items"]], ["cox-arch"])
        self.assertEqual(by_host["filters"], {"host": "codex", "role": None, "enabled": None})

        by_role = list_registry_agents(self.conn, registry_id=REGISTRY_ID, role="skill-worker")
        self.assertEqual([i["agentId"] for i in by_role["items"]], ["dev-wkb1"])

        by_enabled = list_registry_agents(self.conn, registry_id=REGISTRY_ID, enabled=False)
        self.assertEqual([i["agentId"] for i in by_enabled["items"]], ["cox-arch"])

        combined = list_registry_agents(
            self.conn, registry_id=REGISTRY_ID, host="codex", role="sales-architect", enabled=False
        )
        self.assertEqual([i["agentId"] for i in combined["items"]], ["cox-arch"])

        none_match = list_registry_agents(
            self.conn, registry_id=REGISTRY_ID, host="workbuddy", role="release-ops"
        )
        self.assertEqual(none_match["items"], [])
        self.assertEqual(none_match["schemaVersion"], "registry-agent-list.v1")

    def test_empty_candidate_library(self):
        """已迁移但无成员：items=[]，不当作未迁移。"""
        path = Path(self.tmp.name) / "empty.sqlite3"
        store = _make_legacy(path)
        try:
            apply_registry_candidate_migration(
                store.conn, registry_id=REGISTRY_ID, source_digest=SOURCE_DIGEST
            )
            result = list_registry_agents(store.conn, registry_id=REGISTRY_ID)
            self.assertEqual(result["items"], [])
            self.assertEqual(result["registry"]["authority"], "candidate")
        finally:
            store.close()

    def test_member_without_projects_and_checks(self):
        _insert_env(self.conn, env_id="env-9", rev=1, host_type="qclaw", instance_id="inst-9",
                    adapter_type="qclaw", connector="conn-9")
        _insert_model(self.conn, mp_id="mp-9", mp_rev=1, env_id="env-9", env_rev=1)
        _seed_cycle(
            self.conn,
            {"agent_id": "solo-1", "env_id": "env-9", "mp_id": "mp-9", "roles": '["qa-reporter"]'},
            {"agent_id": "solo-1", "env_id": "env-9", "env_rev": 1, "adapter_type": "qclaw",
             "connector": "conn-9", "host_agent_id": "host-solo"},
        )
        result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        solo = self.items_by_id(result)["solo-1"]
        self.assertEqual(solo["projects"], [])
        self.assertEqual(solo["checks"], [])


class ConnectionAndTransactionTests(RegistryReadTestCase):
    def test_accepts_tuple_and_row_factory(self):
        as_row = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        self.conn.row_factory = None
        try:
            as_tuple = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        finally:
            self.conn.row_factory = sqlite3.Row
        self.assertEqual(as_row, as_tuple)

    def test_rejects_other_row_factory(self):
        self.conn.row_factory = lambda _cursor, row: row
        try:
            code, details = _call(self.conn, registry_id=REGISTRY_ID)
        finally:
            self.conn.row_factory = sqlite3.Row
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "unsupported_row_factory")

    def test_connection_config_untouched(self):
        before_factory = self.conn.row_factory
        before_isolation = self.conn.isolation_level
        before_changes = self.conn.total_changes
        before_dump = _dump_tables(self.conn)
        list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        self.assertIs(self.conn.row_factory, before_factory)
        self.assertEqual(self.conn.isolation_level, before_isolation)
        self.assertEqual(_dump_tables(self.conn), before_dump)
        self.assertFalse(self.conn.in_transaction)
        # 只读：不产生任何写入（total_changes 不变）。
        self.assertEqual(self.conn.total_changes, before_changes)

    def test_rejects_caller_transaction_and_keeps_it(self):
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute("INSERT INTO sources (source_id, label, registered_at) VALUES ('src-tx','tx',?)", (TS,))
        try:
            code, details = _call(self.conn, registry_id=REGISTRY_ID)
        finally:
            self.assertTrue(self.conn.in_transaction, "caller 事务必须原样保留")
            self.conn.execute("ROLLBACK")
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "connection_in_transaction")
        self.assertFalse(
            self.conn.execute("SELECT 1 FROM sources WHERE source_id = 'src-tx'").fetchone(),
            "caller 未提交事务不得被服务提交",
        )
        self.assertFalse(self.conn.in_transaction)

    def test_read_only_connection_succeeds(self):
        store = SqliteStore(self.path, read_only=True)
        try:
            first = list_registry_agents(store.conn, registry_id=REGISTRY_ID)
            second = list_registry_agents(store.conn, registry_id=REGISTRY_ID)
        finally:
            store.close()
        self.assertEqual(len(first["items"]), 2)
        self.assertEqual(first, second)

    def test_second_call_identical(self):
        first = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        second = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(first, second)

    def test_trace_only_reads_and_own_transaction(self):
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue(seen, "trace 必须捕获到语句")
        for statement in seen:
            head = statement.strip().split(" ")[0].upper()
            self.assertIn(head, ("SELECT", "BEGIN", "COMMIT", "ROLLBACK"), statement)
            self.assertNotIn("PRAGMA", statement.upper())


class ArgumentValidationTests(RegistryReadTestCase):
    def _assert_invalid_argument(self, field, **kwargs):
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            code, details = _call(self.conn, **kwargs)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "invalid_argument")
        self.assertEqual(details["field"], field)
        self.assertEqual(seen, [], "非法入参不得执行任何查询")
        self.assertFalse(self.conn.in_transaction)

    def test_missing_or_bad_connection(self):
        with self.assertRaises(ContractError) as ctx:
            list_registry_agents(None, registry_id=REGISTRY_ID)
        self.assertEqual(ctx.exception.details["reason"], "connection_required")
        with self.assertRaises(ContractError) as ctx:
            list_registry_agents("conn", registry_id=REGISTRY_ID)
        self.assertEqual(ctx.exception.details["reason"], "connection_required")

    def test_registry_id_variants(self):
        for bad in (None, "", "reg-1\n", "reg 1", "reg-1 ", 1, b"reg-1", "../reg-1", "reg-1/../x"):
            self._assert_invalid_argument("registryId", registry_id=bad)

    def test_host_variants(self):
        for bad in ("", "workbuddy\n", " workbuddy", 1, True, "unknown host", "codex/"):
            self._assert_invalid_argument("host", registry_id=REGISTRY_ID, host=bad)

    def test_role_variants(self):
        for bad in ("", "skill-worker\n", "seller", 0, True, ["skill-worker"], "Skill-Worker"):
            self._assert_invalid_argument("role", registry_id=REGISTRY_ID, role=bad)

    def test_enabled_variants(self):
        for bad in (0, 1, "true", "false", "", [], 2):
            self._assert_invalid_argument("enabled", registry_id=REGISTRY_ID, enabled=bad)

    def test_valid_null_filters_accepted(self):
        result = list_registry_agents(
            self.conn, registry_id=REGISTRY_ID, host=None, role=None, enabled=None
        )
        self.assertEqual(len(result["items"]), 2)
        self.assertEqual(result["filters"], {"host": None, "role": None, "enabled": None})


class SchemaStateTests(RegistryReadTestCase):
    def test_unmigrated_rejected(self):
        path = Path(self.tmp.name) / "unmigrated.sqlite3"
        store = _make_legacy(path)
        try:
            code, details = _call(store.conn, registry_id=REGISTRY_ID)
        finally:
            store.close()
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "registry_unmigrated")

    def test_wrong_registry_id_keeps_inspect_semantics(self):
        code, details = _call(self.conn, registry_id="reg-2")
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["field"], "registryId")
        self.assertEqual(details["reason"], "registry_id_mismatch")

    def test_drifted_schema_rejected(self):
        """删掉一个不可变 trigger 造成形态漂移：沿用 inspect 的拒绝，不改其语义。"""
        self.conn.execute("DROP TRIGGER registry_environments_no_update")
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_trigger")
        self.assertFalse(self.conn.in_transaction)


class ReferenceIntegrityTests(RegistryReadTestCase):
    """不可变表（environments / model_profiles / agent_bindings / agent_checks）只能 INSERT，
    所以“错版本 / 缺引用”负例一律在 FK 关闭时用 INSERT + 可变表 registry_agents 指针构造。"""

    def _fk_off(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")

    def _fk_on(self):
        self.conn.execute("PRAGMA foreign_keys = ON")

    def test_missing_legacy_agent(self):
        self._fk_off()
        try:
            self.conn.execute("DELETE FROM agents WHERE agent_id = 'dev-wkb1'")
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.dev-wkb1.agentId")

    def test_missing_environment_revision(self):
        self._fk_off()
        try:
            self.conn.execute(
                "UPDATE registry_agents SET environment_revision = 2 WHERE agent_id = 'dev-wkb1'"
            )
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.dev-wkb1.environment")

    def test_missing_binding(self):
        self._fk_off()
        try:
            self.conn.execute(
                "UPDATE registry_agents SET current_binding_revision = 2 WHERE agent_id = 'dev-wkb1'"
            )
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.dev-wkb1.binding")

    def test_binding_adapter_mismatch(self):
        _insert_binding(self.conn, agent_id="dev-wkb1", binding_rev=2, env_id="env-1",
                        env_rev=1, adapter_type="other-adapter", connector="conn-1",
                        host_agent_id="host-wkb1")
        self.conn.execute(
            "UPDATE registry_agents SET current_binding_revision = 2 WHERE agent_id = 'dev-wkb1'"
        )
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "reference_mismatch")
        self.assertEqual(details["field"], "agents.dev-wkb1.binding")

    def test_missing_model_profile(self):
        self._fk_off()
        try:
            self.conn.execute(
                "UPDATE registry_agents SET model_profile_id = 'mp-x' WHERE agent_id = 'dev-wkb1'"
            )
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.dev-wkb1.modelProfile")

    def test_model_environment_mismatch(self):
        self._fk_off()
        try:
            _insert_model(self.conn, mp_id="mp-3", mp_rev=1, env_id="env-1", env_rev=2)
            self.conn.execute(
                "UPDATE registry_agents SET model_profile_id = 'mp-3' WHERE agent_id = 'dev-wkb1'"
            )
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(details["reason"], "reference_mismatch")
        self.assertEqual(details["field"], "agents.dev-wkb1.modelProfile")

    def test_check_missing_binding(self):
        self._fk_off()
        try:
            _insert_check(self.conn, check_id="chk-bad-binding", agent_id="dev-wkb1",
                          binding_rev=7, env_id="env-1", env_rev=1)
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.dev-wkb1.checks.chk-bad-binding.bindingRevision")

    def test_check_missing_model(self):
        self._fk_off()
        try:
            _insert_check(self.conn, check_id="chk-bad-model", agent_id="dev-wkb1",
                          env_id="env-1", env_rev=1, mp_id="mp-x", mp_rev=1)
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.dev-wkb1.checks.chk-bad-model.modelProfileId")


class PersistedValueTests(RegistryReadTestCase):
    def _corrupt(self, column, value, agent_id="dev-wkb1"):
        """registry_agents 是可变表，可直接改坏所投影字段（CHECK 拦不住的字段）。"""
        self.conn.execute(
            "UPDATE registry_agents SET %s = ? WHERE agent_id = ?" % column, (value, agent_id)
        )
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "invalid_persisted_value")
        self.assertTrue(details["field"].startswith("agents.%s." % agent_id), details["field"])

    def test_bad_json_forms(self):
        self._corrupt("roles_json", "{not-json")
        self._corrupt("roles_json", '"skill-worker"')
        self._corrupt("roles_json", "{}")
        self._corrupt("permission_ceiling_json",
                      '{"writeScope": [], "writeScope": [], "allowNetwork": false}')
        self._corrupt("permission_ceiling_json",
                      '{"writeScope": [], "allowNetwork": NaN, "allowBusinessWrite": false,'
                      ' "allowGit": false, "allowPublish": false}')
        self._corrupt("capabilities_json", "[" * 5000 + "]" * 5000)
        self._corrupt("allowed_roots_json", json.dumps(["a" * (1024 * 1024 + 8)]))

    def test_vocab_and_shape_violations(self):
        self._corrupt("roles_json", "[]")
        self._corrupt("roles_json", '["seller"]')
        self._corrupt("roles_json", '["skill-worker","skill-worker"]')
        self._corrupt("capabilities_json", '["develop","develop"]')
        self._corrupt("allowed_roots_json", "[]")
        self._corrupt("allowed_roots_json", '["relative/path"]')
        self._corrupt("permission_ceiling_json", "{}")
        self._corrupt("permission_ceiling_json",
                      json.dumps({"writeScope": [], "allowNetwork": False,
                                  "allowBusinessWrite": False, "allowGit": True,
                                  "allowPublish": False}))

    def test_scalar_violations(self):
        self._corrupt("agent_alias", "")
        self._corrupt("normalized_alias", "dev-wkb-1")
        self._corrupt("display_name", "   ")
        self._corrupt("created_at", "")
        self._corrupt("updated_at", "")
        self._corrupt("capabilities_json", "null")

    def test_bad_project_roles(self):
        self.conn.execute(
            "UPDATE project_agents SET project_roles_json = '[]' WHERE project_id = 'proj-a'"
        )
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "invalid_persisted_value")
        self.assertEqual(details["field"], "agents.dev-wkb1.projects.proj-a.roles")

    def test_bad_check_requested(self):
        _insert_check(self.conn, check_id="chk-bad-requested", agent_id="dev-wkb1",
                      env_id="env-1", requested="[]")
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "invalid_persisted_value")
        self.assertEqual(details["field"], "agents.dev-wkb1.checks.chk-bad-requested.requested")

    def test_bad_check_observed(self):
        _insert_check(self.conn, check_id="chk-bad-observed", agent_id="dev-wkb1",
                      env_id="env-1", observed="[]")
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(details["reason"], "invalid_persisted_value")
        self.assertEqual(details["field"], "agents.dev-wkb1.checks.chk-bad-observed.observed")

    def test_bad_check_expires_at(self):
        _insert_check(self.conn, check_id="chk-bad-expires", agent_id="dev-wkb1",
                      env_id="env-1", expires_at="")
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(details["reason"], "invalid_persisted_value")
        self.assertEqual(details["field"], "agents.dev-wkb1.checks.chk-bad-expires.expiresAt")

    def test_filter_cannot_hide_corruption(self):
        """损坏成员被过滤条件排除时，仍必须拒绝（先校验全体再过滤）。"""
        self._corrupt("roles_json", "[]")
        code, details = _call(self.conn, registry_id=REGISTRY_ID, host="codex", role="sales-architect")
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["field"], "agents.dev-wkb1.roles")


class SqliteBoundaryTests(RegistryReadTestCase):
    def test_closed_connection_not_wrapped(self):
        self.conn.close()
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        self.assertNotIsInstance(ctx.exception, ContractError)

    def test_authorizer_error_not_wrapped(self):
        self.conn.set_authorizer(_deny_read("registry_agents"))
        try:
            with self.assertRaises(sqlite3.DatabaseError) as ctx:
                list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        finally:
            self.conn.set_authorizer(None)
        self.assertNotIsInstance(ctx.exception, ContractError)
        self.assertFalse(self.conn.in_transaction)

    def test_failure_rolls_back_only_own_transaction(self):
        before = _dump_tables(self.conn)
        before_changes = self.conn.total_changes
        self.conn.execute("UPDATE registry_agents SET roles_json = '[]' WHERE agent_id = 'dev-wkb1'")
        with self.assertRaises(ContractError):
            list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute("UPDATE registry_agents SET roles_json = '[\"skill-worker\"]' WHERE agent_id = 'dev-wkb1'")
        self.assertEqual(_dump_tables(self.conn), before)
        self.assertEqual(self.conn.total_changes, before_changes + 2)


class SnapshotTests(RegistryReadTestCase):
    def test_snapshot_holds_when_writer_commits_between_reads(self):
        """WAL 双连接确定性验证：writer 在 reader 多表查询之间提交，列表仍同快照。"""
        path = Path(self.tmp.name) / "wal.sqlite3"
        store = _make_legacy(path)
        try:
            apply_registry_candidate_migration(
                store.conn, registry_id=REGISTRY_ID, source_digest=SOURCE_DIGEST
            )
            mode = store.conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            _seed_two_members(store.conn)
        finally:
            store.close()
        if mode != "wal":
            self.skipTest("本环境无法切换到 WAL 模式，快照用例跳过（不伪造快照证据）")

        reader = sqlite3.connect(str(path), isolation_level=None)
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA busy_timeout = 5000")
        writer = sqlite3.connect(str(path), isolation_level=None)
        writer.execute("PRAGMA busy_timeout = 5000")
        state = {"fired": False}

        def trace(statement):
            # reader 的读快照在 inspect 的首条读（sqlite_master）时建立；writer 在第二
            # 条读（registry_metadata）时提交，后续 registry_agents 读必须仍见旧 revision。
            if state["fired"] or "FROM registry_metadata" not in statement:
                return
            state["fired"] = True
            writer.execute(
                "UPDATE registry_agents SET revision = 2, updated_at = ? WHERE agent_id = ?",
                (TS_LATER, "dev-wkb1"),
            )

        reader.set_trace_callback(trace)
        try:
            first = list_registry_agents(reader, registry_id=REGISTRY_ID)
        finally:
            reader.set_trace_callback(None)
        self.assertTrue(state["fired"], "writer 必须在 reader 查询之间提交")
        self.assertEqual(self.items_by_id(first)["dev-wkb1"]["revision"], 1)

        second = list_registry_agents(reader, registry_id=REGISTRY_ID)
        self.assertEqual(self.items_by_id(second)["dev-wkb1"]["revision"], 2)
        writer.close()
        reader.close()


class NoHostProbeTests(RegistryReadTestCase):
    def test_service_works_with_host_access_patched_out(self):
        with mock.patch.object(sqlite3, "connect", side_effect=RuntimeError("no connect")), \
                mock.patch("builtins.open", side_effect=RuntimeError("no open")), \
                mock.patch.object(socket, "socket", side_effect=RuntimeError("no socket")), \
                mock.patch.object(subprocess, "Popen", side_effect=RuntimeError("no popen")):
            result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(len(result["items"]), 2)


class HistoricalReferenceBoundaryTests(RegistryReadTestCase):
    """本轮新增（R1 F1 / F2）：历史 check 的环境存在性与 missing / mismatch 区分。

    不可变表只能 INSERT，负例一律 FK 关闭 + INSERT 构造；只用本轮 runtime 内新建的
    SYNTHETIC 库，不复用旧 fixture / development.sqlite，也不改动原 45 个 test_ 方法。
    """

    def _fk_off(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")

    def _fk_on(self):
        self.conn.execute("PRAGMA foreign_keys = ON")

    def _ghost_history(self):
        """合成旧 binding + check 同时指向不存在的 env-ghost（历史环境缺失）。"""
        self._fk_off()
        try:
            _insert_binding(self.conn, "dev-wkb1", binding_rev=99, env_id="env-ghost", env_rev=1)
            _insert_check(self.conn, check_id="chk-ghost", binding_rev=99,
                          env_id="env-ghost", env_rev=1)
        finally:
            self._fk_on()

    def test_historical_environment_missing_rejected(self):
        """binding 行存在不等于它的 environment 存在：精确版本必须真实存在。"""
        self._ghost_history()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.dev-wkb1.checks.chk-ghost.environmentId")

    def test_filtered_member_historical_environment_missing_rejected(self):
        """坏成员被 host 过滤掉也必须拒绝：先全体校验，后 AND 过滤。"""
        self._ghost_history()
        code, details = _call(self.conn, registry_id=REGISTRY_ID, host="codex")
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.dev-wkb1.checks.chk-ghost.environmentId")

    def test_historical_binding_environment_id_mismatch(self):
        """binding 主键真实存在但 check 声明环境不同 → reference_mismatch，不是 missing。"""
        self._fk_off()
        try:
            _insert_check(self.conn, check_id="chk-env-mismatch", binding_rev=1,
                          env_id="env-2", env_rev=1)
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "reference_mismatch")
        self.assertEqual(details["field"],
                         "agents.dev-wkb1.checks.chk-env-mismatch.environmentId")

    def test_historical_binding_environment_revision_mismatch(self):
        """环境版本不符同样是 reference_mismatch（先查主键再核关联，不混报 missing）。"""
        self._fk_off()
        try:
            _insert_binding(self.conn, "dev-wkb1", binding_rev=98, env_id="env-1", env_rev=2)
            _insert_check(self.conn, check_id="chk-rev-mismatch", binding_rev=98,
                          env_id="env-1", env_rev=1)
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "reference_mismatch")
        self.assertEqual(details["field"],
                         "agents.dev-wkb1.checks.chk-rev-mismatch.environmentRevision")

    def test_truly_missing_historical_binding_stays_missing(self):
        """真正没有 binding 主键：仍是 missing_reference，不与 mismatch 混为一谈。"""
        self._fk_off()
        try:
            _insert_check(self.conn, check_id="chk-no-binding", binding_rev=7,
                          env_id="env-1", env_rev=1)
        finally:
            self._fk_on()
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"],
                         "agents.dev-wkb1.checks.chk-no-binding.bindingRevision")


class JsonFiniteValueTests(RegistryReadTestCase):
    """本轮新增（R1 F3）：JSON 解码后不得残留任何非有限 float，有限值原样保留。"""

    def _checks_by_id(self, result):
        return {c["checkId"]: c for c in self.items_by_id(result)["dev-wkb1"]["checks"]}

    def _reject(self, check_id, part, requested="{}", observed=None):
        _insert_check(self.conn, check_id=check_id, requested=requested, observed=observed)
        code, details = _call(self.conn, registry_id=REGISTRY_ID)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "invalid_persisted_value")
        self.assertEqual(details["field"], "agents.dev-wkb1.checks.%s.%s" % (check_id, part))
        # 不回显 JSON 正文：details 只有稳定 field / reason。
        rendered = json.dumps(details, ensure_ascii=False)
        self.assertNotIn("1e999", rendered)
        self.assertNotIn("Infinity", rendered)
        self.assertNotIn("NaN", rendered)

    def test_requested_positive_exponent_overflow(self):
        self._reject("chk-pos", "requested", requested='{"n":1e999}')

    def test_requested_negative_exponent_overflow(self):
        self._reject("chk-neg", "requested", requested='{"n":-1e999}')

    def test_observed_nested_exponent_overflow(self):
        """嵌套 object / list 里的溢出值同样拒绝（覆盖 observed）。"""
        self._reject("chk-nested", "observed", observed='{"x":[{"n":1e999}]}')

    def test_requested_literal_infinity_still_rejected(self):
        """字面 Infinity 的旧拒绝不削弱（parse_constant 路径保持可用）。"""
        self._reject("chk-literal", "requested", requested='{"n":Infinity}')

    def test_finite_large_exponent_accepted(self):
        _insert_check(self.conn, check_id="chk-finite", requested='{"n":1e308}')
        result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        value = self._checks_by_id(result)["chk-finite"]["requested"]["n"]
        self.assertIsInstance(value, float)
        self.assertEqual(value, 1e308)
        self.assertTrue(math.isfinite(value))

    def test_normal_values_and_literal_strings_preserved(self):
        """有限浮点 / 负浮点 / 大整数 / null / 数组 / 对象与字面字符串全部保持原值。"""
        payload = json.dumps({
            "finite": 1e308,
            "negative": -1.5,
            "zero": 0,
            "bigInt": 123456789012345678901234567890,
            "nullValue": None,
            "textOverflow": "1e999",
            "textInfinity": "Infinity",
            "array": [1, -2.5, "1e999"],
            "object": {"nested": 1e308},
        })
        _insert_check(self.conn, check_id="chk-mixed", requested=payload, observed=payload)
        result = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        requested = self._checks_by_id(result)["chk-mixed"]["requested"]
        self.assertEqual(requested["finite"], 1e308)
        self.assertEqual(requested["negative"], -1.5)
        self.assertEqual(requested["zero"], 0)
        self.assertEqual(requested["bigInt"], 123456789012345678901234567890)
        self.assertIsNone(requested["nullValue"])
        self.assertEqual(requested["textOverflow"], "1e999")
        self.assertEqual(requested["textInfinity"], "Infinity")
        self.assertEqual(requested["array"], [1, -2.5, "1e999"])
        self.assertEqual(requested["object"], {"nested": 1e308})
        self.assertEqual(self._checks_by_id(result)["chk-mixed"]["observed"], requested)


if __name__ == "__main__":
    unittest.main()
