"""D4-NEXT-02B2 定向测试：候选成员登记服务（预检 + 受信应用最小纵向切片）。

运行（cox 独立运行，工作目录 shared/agent-orchestrator）::

    python3 -B -m unittest discover -s tests -p test_d4_agent_registry_service.py
    python3 -B -m unittest discover -s tests -p 'test_d4*.py'

范围：
- 只新增本测试文件与 service/agent_registry.py；不改旧 Python / __init__ / CLI /
  adapter / schema / 合同 / 迁移器 / 只读服务及其测试，也不改 docs / 名册 / DB。
- 只测本卡新生成的 SYNTHETIC candidate 库（SqliteStore + apply 迁移器生成），
  不使用 development.sqlite / 历史 fixture / 正式库。
- worker 侧 Bash 被宿主拒绝、测试 NOT_RUN；由 cox 在既有权限内独立补验。
- 每个用例写本轮隔离 runtime（TemporaryDirectory 沿 TMPDIR，可用
  D4_AGENT_REGISTRY_RUNTIME_DIR 覆盖），不写历史 fixture 默认 root。
"""
import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import service.agent_registry as agent_registry_service
from contracts.schemas import ContractError
from service.agent_registry import (
    REGISTRY_APPLY_CAPABILITY,
    register_agent,
    set_agent_enabled,
    update_agent,
)
from service.context import CallerContext
from storage.registry_migrations import apply_registry_candidate_migration
from storage.sqlite_store import SqliteStore

TS = "2026-10-09T00:00:00+08:00"

DIGEST_A = "sha256:" + "a" * 64

REGISTRY_ID = "reg-1"
OTHER_REGISTRY_ID = "reg-2"
SOURCE_DIGEST = "sha256:" + "0" * 64

NEW_AGENT = "new-1"
EXISTING_AGENT = "exist-1"
LEGACY_AGENTS = ("dev-wkb1", "cox-arch")

ROOT = "/Users/demo/.hermes/skills/conductor"

RESULT_KEYS = [
    "schemaVersion", "registry", "wouldWrite", "wouldDispatch", "authorityUnchanged",
    "applied", "replayed", "agentId", "normalizedAlias", "enabled", "revision",
    "resolved", "idempotency",
]
RESOLVED_KEYS = [
    "environmentId", "environmentRevision", "modelProfileId", "modelProfileRevision",
    "bindingRevision",
]


def runtime_root():
    """本轮隔离 runtime；默认沿 TMPDIR，可用 D4_AGENT_REGISTRY_RUNTIME_DIR 覆盖。"""
    base = os.environ.get("D4_AGENT_REGISTRY_RUNTIME_DIR")
    root = base if base else tempfile.gettempdir()
    os.makedirs(root, exist_ok=True)
    return root


def _canonical(obj):
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------------------
# 受控上下文构造（身份只来自受控入口注入的 CallerContext）
# ---------------------------------------------------------------------------
def _owner_context(actor_id="cox", synthetic=True, capability=REGISTRY_APPLY_CAPABILITY):
    capabilities = frozenset({capability}) if capability else frozenset()
    return CallerContext(
        actor_id=actor_id,
        role="owner",
        capabilities=capabilities,
        entry="test-trusted-entry",
        synthetic=synthetic,
    )


def _worker_context():
    return CallerContext(
        actor_id="dev-wkb1",
        role="worker",
        capabilities=frozenset(),
        entry="test-worker-entry",
        synthetic=True,
    )


# ---------------------------------------------------------------------------
# 登记请求构造（agent-registration.v1，12 必填字段）
# ---------------------------------------------------------------------------
def _registration(agent_id=NEW_AGENT, alias="New One", env="env-1", mp="mp-1",
                  enabled=False, display="新成员一号"):
    return {
        "schemaVersion": "agent-registration.v1",
        "agentId": agent_id,
        "agentAlias": alias,
        "displayName": display,
        "roles": ["skill-worker"],
        "capabilities": ["develop", "test"],
        "environmentId": env,
        "allowedRoots": [ROOT],
        "permissionCeiling": {
            "writeScope": [ROOT + "/shared/agent-orchestrator"],
            "allowNetwork": False,
            "allowBusinessWrite": False,
            "allowGit": False,
            "allowPublish": False,
        },
        "modelProfileId": mp,
        "binding": {
            "environmentId": env,
            "adapterType": "workbuddy",
            "connectorConfigRef": "conn-1",
            "executionProfileRef": "profile-1",
            "hostAgentId": None,
            "configDigest": DIGEST_A,
        },
        "enabled": enabled,
    }


class _ExpectInvalid:
    """捕获 ContractError 的上下文管理器（沿用 invalid_task + field/reason 口径）。"""

    def __init__(self, field=None, reason=None):
        self.field = field
        self.reason = reason
        self.error = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            raise AssertionError("预期 ContractError 未抛出")
        if not issubclass(exc_type, ContractError):
            return False
        self.error = exc
        assert exc.code == "invalid_task", exc.code
        details = exc.details or {}
        if self.field is not None:
            assert details.get("field") == self.field, details
        if self.reason is not None:
            assert details.get("reason") == self.reason, details
        return True


# ---------------------------------------------------------------------------
# fixture：legacy schema + candidate 迁移 + 环境/模型/既有成员
# ---------------------------------------------------------------------------
# 省略 payload 与显式 payload=None 的区分哨兵：省略时填默认登记；显式 None
# 原样下传给 register_agent，由合同层以 field=payload 拒绝。
_PAYLOAD_OMITTED = object()
def _make_candidate(path, *, migrate=True, env_revs=(1, 2), model_pairs=((1, 1), (2, 2)),
                    seed_member=False, extra_model_on_env1=None):
    """生成 SYNTHETIC candidate 库。

    - 旧 agents 预置 LEGACY_AGENTS 两行（供旧库不变断言）。
    - environments：env-1 各 revision；model_profiles：mp-1 @ (env_rev, profile_rev)。
    - seed_member=True 时预置一个既有成员（agent_id=EXISTING_AGENT，enabled=0），
      供重复 agentId / 重复 normalizedAlias 冲突用例。
    """
    store = SqliteStore(path)
    conn = store.conn
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO sources (source_id, label, registered_at) VALUES ('src-1','本地来源',?)",
        (TS,),
    )
    for agent_id in LEGACY_AGENTS:
        conn.execute(
            "INSERT INTO agents (agent_id, label, note, registered_at) VALUES (?,?,?,?)",
            (agent_id, "旧成员 " + agent_id, None, TS),
        )
    if seed_member:
        # registry_agents.agent_id 有指向旧 agents 的外键：seed 成员必须先有旧行。
        conn.execute(
            "INSERT INTO agents (agent_id, label, note, registered_at) VALUES (?,?,?,?)",
            (EXISTING_AGENT, "既有成员", None, TS),
        )
    conn.execute(
        "INSERT INTO tasks (task_id, project_id, title, status, revision, created_at, updated_at)"
        " VALUES ('task-legacy-1','d4-product','旧任务','closed',1,?,?)", (TS, TS)
    )
    if not migrate:
        # 未迁移分支：raw 库只有 sources/agents/tasks 三张 legacy 表，
        # environments / model_profiles / registry_agents 等候选表尚不存在，
        # 不得插入（也不能在此建表，否则 registry_unmigrated 用例失真）。
        # 直接返回，由服务层在登记预检时以 registry_unmigrated 拒绝。
        return store
    apply_registry_candidate_migration(
        conn, registry_id=REGISTRY_ID, source_digest=SOURCE_DIGEST
    )
    for rev in env_revs:
        conn.execute(
            "INSERT INTO environments (environment_id, revision, host_type, instance_id,"
            " host_version, adapter_type, connector_config_ref, config_digest,"
            " allowed_roots_json, permission_ceiling_json, enabled, created_by_json, created_at)"
            " VALUES ('env-1',?,'workbuddy',?, '1.4.0','workbuddy','conn-1',?,?,?,?,?,?)",
            (rev, "inst-%d" % rev, DIGEST_A, _canonical([ROOT]), _canonical(
                {
                    "writeScope": [ROOT + "/shared/agent-orchestrator"],
                    "allowNetwork": False,
                    "allowBusinessWrite": False,
                    "allowGit": False,
                    "allowPublish": False,
                }
            ), 1, _canonical({"actorId": "cox"}), TS),
        )
    for env_rev, profile_rev in model_pairs:
        conn.execute(
            "INSERT INTO model_profiles (model_profile_id, profile_revision, environment_id,"
            " environment_revision, strategy, provider, model_id, config_digest,"
            " created_by_json, created_at) VALUES ('mp-1',?,'env-1',?,'fixed',"
            "'synthetic-provider','synthetic-model',?,?,?)",
            (profile_rev, env_rev, DIGEST_A, _canonical({"actorId": "cox"}), TS),
        )
    if extra_model_on_env1 is not None:
        conn.execute(
            "INSERT INTO model_profiles (model_profile_id, profile_revision, environment_id,"
            " environment_revision, strategy, provider, model_id, config_digest,"
            " created_by_json, created_at) VALUES (?,?, 'env-1',1,'fixed',"
            "'synthetic-provider','synthetic-model',?,?,?)",
            (extra_model_on_env1, 1, DIGEST_A, _canonical({"actorId": "cox"}), TS),
        )
    if seed_member:
        _seed_member(conn, agent_id=EXISTING_AGENT, normalized_alias=EXISTING_AGENT)
    return store


def _seed_member(conn, agent_id, normalized_alias, enabled=0):
    """插入一条既有候选成员 + revision 1 绑定（不产生审计行，仅供冲突用例）。

    连接为 autocommit（SqliteStore 用 isolation_level=None）：`with conn:`
    不会开启事务，两行必须用显式 BEGIN IMMEDIATE 包裹；任一 INSERT 失败
    整体 ROLLBACK，避免留下只有 agent_bindings 外键行的半截 fixture。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id,"
            " environment_revision, adapter_type, connector_config_ref, execution_profile_ref,"
            " host_agent_id, config_digest, created_by_json, created_at)"
            " VALUES (?,1,'env-1',1,'workbuddy','conn-1','profile-1',NULL,?,?,?)",
            (agent_id, DIGEST_A, _canonical({"actorId": "cox"}), TS),
        )
        conn.execute(
            "INSERT INTO registry_agents (agent_id, agent_alias, normalized_alias, display_name,"
            " roles_json, capabilities_json, environment_id, environment_revision,"
            " allowed_roots_json, permission_ceiling_json, model_profile_id, model_profile_revision,"
            " current_binding_revision, enabled, registration_complete, revision, source_row_json,"
            " created_by_json, created_at, updated_at)"
            " VALUES (?,?,?,'既有成员','[\"skill-worker\"]','[\"develop\"]','env-1',1,?,?,"
            "'mp-1',1,1,?,1,1,NULL,?,?,?)",
            (
                agent_id, agent_id, normalized_alias, _canonical([ROOT]), _canonical(
                    {
                        "writeScope": [ROOT + "/shared/agent-orchestrator"],
                        "allowNetwork": False,
                        "allowBusinessWrite": False,
                        "allowGit": False,
                        "allowPublish": False,
                    }
                ), enabled, _canonical({"actorId": "cox"}), TS, TS,
            ),
        )
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _snapshot_master(conn):
    # fetchall() 可能返回 sqlite3.Row（服务/用例可设 row_factory），Row 之间
    # 不可比较，直接 sorted 会抛 TypeError。先转普通值元组再排序，保证快照
    # 恒可排序；type/name 均为 TEXT，转换不改断言语义。
    return sorted(
        tuple(row) for row in
        conn.execute("SELECT type, name FROM sqlite_master").fetchall()
    )


def _table_counts(conn):
    tables = (
        "agents", "registry_agents", "agent_bindings", "registry_changes",
        "environments", "model_profiles",
    )
    return {
        name: conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
        for name in tables
    }


def _legacy_rows(conn):
    """旧库三张账本表逐行快照（旧库不变断言用）。"""
    return {
        "sources": conn.execute("SELECT * FROM sources").fetchall(),
        "tasks": conn.execute("SELECT * FROM tasks").fetchall(),
        "agents": conn.execute("SELECT * FROM agents ORDER BY agent_id").fetchall(),
    }


class _CandidateCase(unittest.TestCase):
    """公共 setUp/tearDown：每用例独立临时库，不写历史 fixture 默认 root。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            prefix="d4-agent-registry-", dir=runtime_root()
        )
        self.path = os.path.join(self._tmp.name, "candidate.sqlite")
        self.store = _make_candidate(self.path, seed_member=True,
                                     extra_model_on_env1="mp-9")
        self.conn = self.store.conn

    def tearDown(self):
        try:
            if self.conn.in_transaction:
                self.conn.execute("ROLLBACK")
        finally:
            self.conn.close()
            self._tmp.cleanup()

    def register(self, payload=None, apply=False, caller=None, **kwargs):
        return register_agent(
            self.conn,
            registry_id=kwargs.pop("registry_id", REGISTRY_ID),
            caller=_owner_context() if caller is None else caller,
            payload=_registration() if payload is None else payload,
            idempotency_key=kwargs.pop("idempotency_key", "idem-1"),
            apply=apply,
            **kwargs,
        )


# ---------------------------------------------------------------------------
# 入参与权限拒绝（先于任何查询 / 事务）
# ---------------------------------------------------------------------------
class ArgAndAuthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(
            prefix="d4-agent-registry-arg-", dir=runtime_root()
        )
        self.conn = sqlite3.connect(":memory:")

    def tearDown(self):
        self.conn.close()
        self._tmp.cleanup()

    def _call(self, caller=_owner_context(), payload=_PAYLOAD_OMITTED, **kwargs):
        return register_agent(
            self.conn,
            registry_id=kwargs.pop("registry_id", REGISTRY_ID),
            caller=caller,
            payload=_registration() if payload is _PAYLOAD_OMITTED else payload,
            idempotency_key=kwargs.pop("idempotency_key", "idem-1"),
            apply=kwargs.pop("apply", False),
            **kwargs,
        )

    def test_missing_context_rejected(self):
        with self.assertRaises(ContractError) as caught:
            self._call(caller=None)
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_non_caller_context_rejected(self):
        with self.assertRaises(ContractError) as caught:
            self._call(caller=object())
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_bad_registry_id_rejected(self):
        for bad in (None, "", "reg-1\n", " reg-1", "/a/reg-1", 1, True):
            with self.assertRaises(ContractError) as caught:
                self._call(registry_id=bad)
            details = caught.exception.details or {}
            self.assertEqual(details.get("field"), "registryId")

    def test_bad_idempotency_key_rejected(self):
        for bad in (None, "", "idem-1\n", "a b", "../idem", 1, True, []):
            with self.assertRaises(ContractError) as caught:
                self._call(idempotency_key=bad)
            details = caught.exception.details or {}
            self.assertEqual(details.get("field"), "idempotencyKey")

    def test_payload_not_dict_rejected(self):
        # None 显式传入时原样下传（sentinel 区分省略），由合同层拒绝。
        for bad in (None, "json-text", 1, [], True):
            with self.assertRaises(ContractError) as caught:
                self._call(payload=bad)
            details = caught.exception.details or {}
            self.assertEqual(details.get("field"), "payload")

    def test_invalid_registration_rejected(self):
        # 首次登记 enabled=true 抢占启用动作：合同层冻结拒绝。
        with self.assertRaises(ContractError) as caught:
            self._call(payload=_registration(enabled=True))
        details = caught.exception.details or {}
        self.assertEqual(details.get("field"), "enabled")

        # 未知字段（含 payload 自报 createdBy 冒充身份）一律拒绝。
        payload = _registration()
        payload["createdBy"] = "attacker"
        with self.assertRaises(ContractError):
            self._call(payload=payload)

    def test_expected_revision_not_applicable_to_register(self):
        with self.assertRaises(ContractError) as caught:
            self._call(expected_revision=1)
        details = caught.exception.details or {}
        self.assertEqual(details.get("reason"), "expected_revision_not_applicable")

    def test_connection_in_transaction_rejected(self):
        self.conn.execute("BEGIN")
        try:
            with self.assertRaises(ContractError) as caught:
                self._call()
            details = caught.exception.details or {}
            self.assertEqual(details.get("reason"), "connection_in_transaction")
        finally:
            self.conn.execute("ROLLBACK")

    def test_apply_requires_owner_role(self):
        with self.assertRaises(ContractError) as caught:
            self._call(apply=True, caller=_worker_context())
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_apply_requires_capability(self):
        with self.assertRaises(ContractError) as caught:
            self._call(apply=True, caller=_owner_context(capability=None))
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_update_agent_explicitly_rejected(self):
        with self.assertRaises(ContractError) as caught:
            update_agent(
                self.conn, registry_id=REGISTRY_ID, caller=_owner_context(),
                agent_id=NEW_AGENT, payload=_registration(),
                idempotency_key="idem-u1", expected_revision=1,
            )
        details = caught.exception.details or {}
        self.assertEqual(details.get("reason"), "not_implemented_scope")

    def test_set_agent_enabled_explicitly_rejected(self):
        for enabled in (True, False):
            with self.assertRaises(ContractError) as caught:
                set_agent_enabled(
                    self.conn, registry_id=REGISTRY_ID, caller=_owner_context(),
                    agent_id=NEW_AGENT, enabled=enabled,
                    expected_revision=1, apply=True,
                )
            details = caught.exception.details or {}
            self.assertEqual(details.get("reason"), "not_implemented_scope")


# ---------------------------------------------------------------------------
# 预检：零写入投影与受控拒绝
# ---------------------------------------------------------------------------
class PrecheckTests(_CandidateCase):
    def test_precheck_success_projection(self):
        result = self.register()
        self.assertEqual(sorted(result), sorted(RESULT_KEYS))
        self.assertEqual(result["schemaVersion"], "registry-agent-register.v1")
        self.assertFalse(result["wouldWrite"])
        self.assertFalse(result["wouldDispatch"])
        self.assertTrue(result["authorityUnchanged"])
        self.assertFalse(result["applied"])
        self.assertFalse(result["replayed"])
        self.assertFalse(result["enabled"])
        self.assertEqual(result["revision"], 1)
        self.assertEqual(result["agentId"], NEW_AGENT)
        self.assertEqual(result["normalizedAlias"], "new one")
        self.assertEqual(
            sorted(result["resolved"]),
            sorted(RESOLVED_KEYS),
        )
        # 解析到最新 environment / model profile revision。
        self.assertEqual(result["resolved"]["environmentRevision"], 2)
        self.assertEqual(result["resolved"]["modelProfileRevision"], 2)
        self.assertEqual(result["resolved"]["bindingRevision"], 1)
        # registry 块直接来自 inspect：authority 仍为 candidate。
        self.assertEqual(result["registry"]["authority"], "candidate")
        self.assertEqual(result["registry"]["registryId"], REGISTRY_ID)
        self.assertEqual(result["idempotency"]["key"], "idem-1")
        self.assertTrue(result["idempotency"]["contentDigest"].startswith("sha256:"))

    def test_precheck_writes_nothing(self):
        master_before = _snapshot_master(self.conn)
        counts_before = _table_counts(self.conn)
        changes_before = self.conn.total_changes
        self.register()
        self.assertEqual(self.conn.total_changes, changes_before)
        self.assertEqual(_snapshot_master(self.conn), master_before)
        self.assertEqual(_table_counts(self.conn), counts_before)
        self.assertFalse(self.conn.in_transaction)
        # 不创建表、不更新库：registry_changes 仍为空。
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM registry_changes").fetchone()[0], 0)

    def test_precheck_unknown_environment_rejected(self):
        before = _table_counts(self.conn)
        with _ExpectInvalid(field="environmentId", reason="unknown_environment"):
            self.register(payload=_registration(env="env-x"))
        self.assertEqual(_table_counts(self.conn), before)

    def test_precheck_unknown_model_profile_rejected(self):
        before = _table_counts(self.conn)
        # mp-9 只存在于 env-1 revision 1；登记解析到最新 env revision 2，无兜底。
        with _ExpectInvalid(field="modelProfileId", reason="unknown_model_profile"):
            self.register(payload=_registration(mp="mp-9"))
        self.assertEqual(_table_counts(self.conn), before)

    def test_precheck_duplicate_agent_rejected(self):
        with _ExpectInvalid(field="agentId", reason="duplicate_agent"):
            self.register(payload=_registration(agent_id=EXISTING_AGENT))

    def test_precheck_duplicate_alias_rejected(self):
        # 停用成员（enabled=0）的 normalizedAlias 同样保留占用。
        with _ExpectInvalid(field="agentAlias", reason="duplicate_alias"):
            self.register(payload=_registration(alias="EXIST-1"))

    def test_precheck_unmigrated_rejected(self):
        with tempfile.TemporaryDirectory(
            prefix="d4-agent-registry-raw-", dir=runtime_root()
        ) as raw_dir:
            raw_store = _make_candidate(
                os.path.join(raw_dir, "raw.sqlite"), migrate=False
            )
            try:
                with _ExpectInvalid(field="registry", reason="registry_unmigrated"):
                    register_agent(
                        raw_store.conn,
                        registry_id=REGISTRY_ID,
                        caller=_owner_context(),
                        payload=_registration(),
                        idempotency_key="idem-1",
                    )
            finally:
                raw_store.conn.close()

    def test_precheck_registry_id_mismatch_rejected(self):
        with self.assertRaises(ContractError):
            self.register(registry_id=OTHER_REGISTRY_ID)

    def test_precheck_row_factory_row_accepted(self):
        self.conn.row_factory = sqlite3.Row
        result = self.register()
        self.assertFalse(result["wouldWrite"])

    def test_precheck_no_external_access(self):
        with mock.patch("builtins.open", side_effect=AssertionError("open called")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket called")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("popen called")):
            result = self.register()
        self.assertFalse(result["wouldWrite"])


# ---------------------------------------------------------------------------
# 受信应用：原子写入、幂等、冲突、回滚、旧库不变
# ---------------------------------------------------------------------------
class ApplyTests(_CandidateCase):
    def test_apply_success_atomic_writes(self):
        before = _legacy_rows(self.conn)
        result = self.register(apply=True)
        self.assertTrue(result["applied"])
        self.assertTrue(result["wouldWrite"])
        self.assertFalse(result["wouldDispatch"])
        self.assertFalse(result["replayed"])
        self.assertTrue(result["authorityUnchanged"])

        counts = _table_counts(self.conn)
        # 旧 agents 恰好新增被登记 ID 一行；其余旧账本不变。
        self.assertEqual(counts["agents"], len(before["agents"]) + 1)
        self.assertEqual(
            self.conn.execute("SELECT * FROM tasks").fetchall(), before["tasks"]
        )
        self.assertEqual(
            self.conn.execute("SELECT * FROM sources").fetchall(), before["sources"]
        )
        self.assertEqual(counts["registry_agents"], 2)  # seed + 新成员
        self.assertEqual(counts["agent_bindings"], 2)
        self.assertEqual(counts["registry_changes"], 1)

        row = self.conn.execute(
            "SELECT enabled, registration_complete, revision, current_binding_revision,"
            " environment_revision, model_profile_revision, source_row_json, created_by_json"
            " FROM registry_agents WHERE agent_id = ?", (NEW_AGENT,)
        ).fetchone()
        self.assertEqual(row[0], 0)  # 首次登记 enabled 严格 false
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], 1)
        self.assertEqual(row[3], 1)
        self.assertEqual(row[4], 2)  # 解析到最新 env revision
        self.assertEqual(row[5], 2)
        expected_source = _registration()
        expected_source["normalizedAlias"] = "new one"  # validator 生成的派生字段
        self.assertEqual(row[6], _canonical(expected_source))
        self.assertEqual(json.loads(row[7])["synthetic"], True)

        binding = self.conn.execute(
            "SELECT binding_revision, environment_id, environment_revision, adapter_type,"
            " connector_config_ref, execution_profile_ref, host_agent_id, config_digest"
            " FROM agent_bindings WHERE agent_id = ?", (NEW_AGENT,)
        ).fetchone()
        self.assertEqual(binding[0], 1)
        self.assertEqual(binding[1], "env-1")
        self.assertEqual(binding[2], 2)
        self.assertEqual(binding[3], "workbuddy")
        self.assertEqual(binding[4], "conn-1")
        self.assertEqual(binding[5], "profile-1")
        self.assertIsNone(binding[6])
        self.assertEqual(binding[7], DIGEST_A)

        audit = self.conn.execute(
            "SELECT idempotency_key, content_digest, action, object_type, object_id,"
            " before_revision, after_revision FROM registry_changes"
        ).fetchone()
        self.assertEqual(audit[0], "idem-1")
        self.assertEqual(audit[1], result["idempotency"]["contentDigest"])
        self.assertEqual(audit[2], "register")
        self.assertEqual(audit[3], "agent")
        self.assertEqual(audit[4], NEW_AGENT)
        self.assertIsNone(audit[5])
        self.assertEqual(audit[6], 1)

        self.assertEqual(self.conn.execute("PRAGMA foreign_key_check").fetchall(), [])

        new_legacy = self.conn.execute(
            "SELECT label, note, registered_at FROM agents WHERE agent_id = ?",
            (NEW_AGENT,),
        ).fetchone()
        self.assertEqual(new_legacy[0], "新成员一号")
        self.assertIsNone(new_legacy[1])

    def test_apply_preexisting_legacy_agent_preserved(self):
        self.conn.execute(
            "INSERT INTO agents (agent_id, label, note, registered_at)"
            " VALUES (?,?,?,?)", (NEW_AGENT, "旧label", "旧note", TS)
        )
        before = self.conn.execute(
            "SELECT label, note, registered_at FROM agents WHERE agent_id = ?",
            (NEW_AGENT,),
        ).fetchone()
        self.register(apply=True)
        after = self.conn.execute(
            "SELECT label, note, registered_at FROM agents WHERE agent_id = ?",
            (NEW_AGENT,),
        ).fetchone()
        self.assertEqual(after, before)  # 既有旧行绝不改写

    def test_apply_idempotent_replay_same_key_same_content(self):
        first = self.register(apply=True)
        counts_after_first = _table_counts(self.conn)
        audit_before = self.conn.execute(
            "SELECT created_at, result_json FROM registry_changes"
        ).fetchone()
        second = self.register(apply=True)
        self.assertTrue(second["applied"])
        self.assertTrue(second["replayed"])
        self.assertEqual(second["agentId"], first["agentId"])
        self.assertEqual(_table_counts(self.conn), counts_after_first)
        self.assertEqual(
            self.conn.execute(
                "SELECT created_at, result_json FROM registry_changes"
            ).fetchone(),
            audit_before,
        )

    def test_apply_idempotency_conflict_same_key_different_content(self):
        self.register(apply=True)
        before = _table_counts(self.conn)
        with _ExpectInvalid(field="idempotencyKey", reason="idempotency_conflict"):
            self.register(apply=True, payload=_registration(display="别的名字"))
        self.assertEqual(_table_counts(self.conn), before)

    def test_apply_duplicate_agent_different_key_rejected(self):
        self.register(apply=True)
        before = _table_counts(self.conn)
        with _ExpectInvalid(field="agentId", reason="duplicate_agent"):
            self.register(apply=True, idempotency_key="idem-2")
        self.assertEqual(_table_counts(self.conn), before)

    def test_apply_worker_context_denied_no_writes(self):
        before = _table_counts(self.conn)
        changes_before = self.conn.total_changes
        with self.assertRaises(ContractError) as caught:
            self.register(apply=True, caller=_worker_context())
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertEqual(self.conn.total_changes, changes_before)
        self.assertEqual(_table_counts(self.conn), before)

    def test_apply_owner_without_capability_denied(self):
        before = _table_counts(self.conn)
        with self.assertRaises(ContractError) as caught:
            self.register(apply=True, caller=_owner_context(capability=None))
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertEqual(_table_counts(self.conn), before)

    def test_apply_foreign_keys_off_rejected(self):
        self.conn.execute("PRAGMA foreign_keys=OFF")
        try:
            before = _table_counts(self.conn)
            with _ExpectInvalid(field="foreign_keys", reason="foreign_keys_off"):
                self.register(apply=True)
            self.assertEqual(_table_counts(self.conn), before)
        finally:
            self.conn.execute("PRAGMA foreign_keys=ON")

    def test_apply_member_insert_failure_rolls_back_everything(self):
        master_before = _snapshot_master(self.conn)
        before = _legacy_rows(self.conn)
        with mock.patch.object(
            agent_registry_service, "_insert_member",
            side_effect=sqlite3.OperationalError("injected failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.register(apply=True)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(_snapshot_master(self.conn), master_before)
        self.assertEqual(_legacy_rows(self.conn), before)
        self.assertEqual(_table_counts(self.conn)["registry_agents"], 1)
        self.assertEqual(_table_counts(self.conn)["agent_bindings"], 1)
        self.assertEqual(_table_counts(self.conn)["registry_changes"], 0)

    def test_apply_audit_insert_failure_rolls_back_everything(self):
        before = _legacy_rows(self.conn)
        with mock.patch.object(
            agent_registry_service, "_insert_audit",
            side_effect=sqlite3.OperationalError("injected audit failure"),
        ):
            with self.assertRaises(sqlite3.OperationalError):
                self.register(apply=True)
        self.assertFalse(self.conn.in_transaction)
        self.assertEqual(_legacy_rows(self.conn), before)
        self.assertEqual(_table_counts(self.conn)["registry_agents"], 1)
        self.assertEqual(_table_counts(self.conn)["registry_changes"], 0)

    def test_history_tables_are_append_only(self):
        self.register(apply=True)
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE agent_bindings SET config_digest = ? WHERE agent_id = ?",
                (DIGEST_A.replace("a", "b"), NEW_AGENT),
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM agent_bindings WHERE agent_id = ?", (NEW_AGENT,))
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute("DELETE FROM registry_changes")
        with self.assertRaises(sqlite3.IntegrityError):
            self.conn.execute(
                "UPDATE registry_changes SET action = 'tampered' WHERE object_id = ?",
                (NEW_AGENT,),
            )
        self.assertEqual(_table_counts(self.conn)["registry_changes"], 1)

    def test_apply_no_external_access(self):
        with mock.patch("builtins.open", side_effect=AssertionError("open called")), \
                mock.patch("socket.socket", side_effect=AssertionError("socket called")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("popen called")):
            result = self.register(apply=True)
        self.assertTrue(result["applied"])

    def test_apply_rejects_expected_revision(self):
        before = _table_counts(self.conn)
        with _ExpectInvalid(reason="expected_revision_not_applicable"):
            self.register(apply=True, expected_revision=1)
        self.assertEqual(_table_counts(self.conn), before)

    def test_precheck_after_apply_reports_replayed_without_writing(self):
        self.register(apply=True)
        counts = _table_counts(self.conn)
        result = self.register()
        self.assertFalse(result["wouldWrite"])
        self.assertFalse(result["applied"])
        self.assertTrue(result["replayed"])
        self.assertEqual(_table_counts(self.conn), counts)


if __name__ == "__main__":
    unittest.main()
