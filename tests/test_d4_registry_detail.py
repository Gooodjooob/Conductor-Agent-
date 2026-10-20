"""D4-NEXT-02B1B 定向测试：候选成员详情只读服务（02B1A 之后的详情边界）。

运行（cox 独立运行，工作目录 shared/agent-orchestrator）::

    python3 -B -m unittest discover -s tests -p test_d4_registry_detail.py
    python3 -B -m unittest discover -s tests -p 'test_d4*.py'

范围：
- 只新增本测试文件与 service/registry_detail.py；不改旧 Python / __init__ / CLI /
  adapter / schema / 合同 / 计划 / 名册 / DB / 配置，也不改 02B1A 列表服务与其测试。
- 只测本卡新生成的 SYNTHETIC candidate 库（SqliteStore + apply 迁移器生成），
  不使用 development.sqlite / 历史 fixture / 正式库。
- worker 侧 Bash 零尝试、测试 NOT_RUN；由 cox 在既有权限内独立补验。
- 每个用例写本轮隔离 runtime（TemporaryDirectory 沿 TMPDIR，可用
  D4_REGISTRY_DETAIL_RUNTIME_DIR 覆盖），不写历史 fixture 默认 root。

本轮补齐（在原断言全部保留的基础上增补，不改任何既有用例）：
- 空历史正例：无 project / 无 check 的成员返回 ``[]``，不凭空造第二份状态。
- 历史排序正例：binding 逆序插入仍按 bindingRevision 升序，环境版本去重后稳定排序。
- 当前引用缺失负例：当前环境 / 当前 binding / 当前 model profile 缺失各自 fail closed。
- 迁移形态负例：trigger 缺失 / 同名放宽 trigger（schema 漂移）稳定拒绝。
- ``observed_json`` 非有限值（NaN / 1e999）同样 fail closed。
"""
import json
import os
import socket
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from contracts.schemas import ContractError
from service.registry_detail import inspect_registry_agent
from storage.registry_migrations import apply_registry_candidate_migration
from storage.sqlite_store import SqliteStore

TS = "2026-10-09T00:00:00+08:00"
TS_LATER = "2026-10-09T01:00:00+08:00"
TS_NEWEST = "2026-10-09T02:00:00+08:00"
TS_OLD = "2026-10-08T23:00:00+08:00"
TS_EXPIRED = "2020-01-01T00:00:00+08:00"

DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64
DIGEST_C = "sha256:" + "c" * 64

REGISTRY_ID = "reg-1"
OTHER_REGISTRY_ID = "reg-2"
SOURCE_DIGEST = "sha256:" + "0" * 64

TARGET = "dev-wkb1"
OTHER = "cox-arch"

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

ROOT_KEYS = [
    "schemaVersion", "registry", "item", "bindingHistory", "environmentHistory",
    "modelProfiles", "wouldWrite", "wouldDispatch", "authorityUnchanged",
]
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
BINDING_HISTORY_KEYS = BINDING_KEYS + ["createdAt"]
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

# 非法入参：空 / 尾 LF / 空白 / 路径 / URL / 控制字符 / 错误类型。
BAD_ARGUMENTS = [
    None, "", "dev-wkb1\n", " dev-wkb1", "dev-wkb1 ", "/Users/demo/dev-wkb1",
    "../dev-wkb1", "https://host/dev-wkb1", "http://host/x#dev-wkb1", "dev wkb1",
    "dev-wkb1\x00", 1, True, ["dev-wkb1"], b"dev-wkb1",
]


def runtime_root():
    """本轮隔离 runtime；默认沿 TMPDIR，可用 D4_REGISTRY_DETAIL_RUNTIME_DIR 覆盖。"""
    base = os.environ.get("D4_REGISTRY_DETAIL_RUNTIME_DIR")
    root = Path(base) if base else Path(tempfile.gettempdir())
    root.mkdir(parents=True, exist_ok=True)
    return root


# -- fixture helper ---------------------------------------------------------
def _make_legacy(path):
    """现有 SqliteStore 生成 legacy schema + 合成旧行（三个旧 agent，供 FK 引用）。"""
    store = SqliteStore(path)
    conn = store.conn
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "INSERT INTO sources (source_id, label, registered_at) VALUES ('src-1','本地来源',?)", (TS,)
    )
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
                    host_agent_id="host-wkb1", digest=DIGEST_A, created_at=TS):
    conn.execute(
        "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id, environment_revision,"
        " adapter_type, connector_config_ref, execution_profile_ref, host_agent_id, config_digest,"
        " created_by_json, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (agent_id, binding_rev, env_id, env_rev, adapter_type, connector, execution_profile_ref,
         host_agent_id, digest, "{}", created_at),
    )


def _seed_cycle(conn, member_kwargs, *binding_kwargs_list):
    """同一事务插 member + 全部 binding（循环 FK，DEFERRABLE INITIALLY DEFERRED）。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        _insert_member(conn, **member_kwargs)
        for kwargs in binding_kwargs_list:
            _insert_binding(conn, **kwargs)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise


def _insert_project_agent(conn, project_id="proj-a", agent_id=TARGET,
                          roles='["skill-worker"]', enabled=1, revision=1):
    conn.execute(
        "INSERT INTO project_agents (project_id, agent_id, project_roles_json,"
        " permission_ceiling_json, enabled, revision, created_by_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, agent_id, roles, CEILING_JSON, enabled, revision, "{}", TS, TS),
    )


def _insert_check(conn, check_id="chk-1", agent_id=TARGET, binding_rev=1, env_id="env-1",
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


def _raw(conn, sql, params):
    """临时关 FK 插入坏引用行（只为构造负例 fixture），随后立刻恢复 ON。"""
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(sql, params)
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _insert_legacy_agent(conn, agent_id, label=None):
    """只补 legacy ``agents`` 行：成员 FK 与 ``_build_item`` 的旧 ID 核对都需要它。"""
    conn.execute(
        "INSERT INTO agents (agent_id, label, note, registered_at) VALUES (?,?,?,?)",
        (agent_id, label or agent_id, "synthetic registry entry", TS),
    )


def _seed_member_with_fk_off(conn, member_kwargs, *binding_kwargs_list):
    """FK 关闭下插入成员 / 绑定，用于构造「当前引用缺失」负例 fixture。

    ``sqlite3`` 在 FK=ON 时无法构造「成员当前引用不存在的行」（复合 FK 会在 COMMIT
    时拒绝），因此本 helper 只在 fixture 构造阶段关闭 FK，插入后立刻恢复 ``ON``；
    服务本身只读投影、插入后不再触发 FK 复核，因此该 fixture 能真实表达
    「已落库但引用已缺失」的损坏形态。
    """
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            _insert_member(conn, **member_kwargs)
            for kwargs in binding_kwargs_list:
                _insert_binding(conn, **kwargs)
            conn.execute("COMMIT")
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _seed_target_member(conn):
    """目标成员 dev-wkb1：两个环境版本 + 两个模型版本 + 两个历史绑定 + 多项目 + 多检查。"""
    _insert_env(conn, env_id="env-1", rev=1, host_type="workbuddy", instance_id="inst-wkb-1",
                host_version="1.4.0", adapter_type="workbuddy", connector="conn-1", digest=DIGEST_A)
    _insert_env(conn, env_id="env-1", rev=2, host_type="workbuddy", instance_id="inst-wkb-2",
                host_version="1.5.0", adapter_type="workbuddy", connector="conn-2", digest=DIGEST_B)
    _insert_model(conn, mp_id="mp-1", mp_rev=1, env_id="env-1", env_rev=1, strategy="fixed",
                  provider="synthetic-provider", model_id="synthetic-model-1", digest=DIGEST_A)
    _insert_model(conn, mp_id="mp-2", mp_rev=1, env_id="env-1", env_rev=2, strategy="fixed",
                  provider="synthetic-provider", model_id="synthetic-model-2", digest=DIGEST_B)

    _seed_cycle(
        conn,
        {"agent_id": TARGET, "alias": "Dev-WKB1", "display": "成员A", "roles": '["skill-worker"]',
         "env_id": "env-1", "env_rev": 2, "mp_id": "mp-2", "mp_rev": 1, "binding_rev": 2,
         "enabled": 1},
        {"agent_id": TARGET, "binding_rev": 1, "env_id": "env-1", "env_rev": 1,
         "adapter_type": "workbuddy", "connector": "conn-1", "host_agent_id": "host-wkb1",
         "digest": DIGEST_A, "created_at": TS_OLD},
        {"agent_id": TARGET, "binding_rev": 2, "env_id": "env-1", "env_rev": 2,
         "adapter_type": "workbuddy", "connector": "conn-2", "host_agent_id": "host-wkb1",
         "digest": DIGEST_B, "created_at": TS},
    )

    _insert_project_agent(conn, project_id="proj-b", agent_id=TARGET, roles='["skill-worker"]')
    _insert_project_agent(conn, project_id="proj-a", agent_id=TARGET,
                          roles='["skill-worker","docs-worker"]')

    # 历史 check：跨两个 binding / 环境 / 模型版本；含 SYNHETIC+passed 与已过期字符串。
    _insert_check(conn, check_id="chk-old", agent_id=TARGET, binding_rev=1, env_id="env-1",
                  env_rev=1, digest=DIGEST_A, mp_id="mp-1", mp_rev=1, check_type="model",
                  status="passed", evidence_level="SYNTHETIC", observed_at=TS_OLD,
                  expires_at=TS_EXPIRED, evidence_ref="evidence://synthetic/old",
                  requested='{"synthetic": true}', observed='{"note":"old"}')
    _insert_check(conn, check_id="chk-current", agent_id=TARGET, binding_rev=2, env_id="env-1",
                  env_rev=2, digest=DIGEST_B, mp_id="mp-2", mp_rev=1, check_type="model",
                  status="passed", evidence_level="OFFICIAL", observed_at=TS_NEWEST,
                  expires_at=TS_LATER, evidence_ref="evidence://official/current",
                  requested='{"ratio": 1e308}', observed='{"note":"current"}')
    _insert_check(conn, check_id="chk-nomodel", agent_id=TARGET, binding_rev=2, env_id="env-1",
                  env_rev=2, digest=DIGEST_B, mp_id=None, mp_rev=None, check_type="config",
                  status="failed", evidence_level="LOCAL", observed_at=TS,
                  expires_at=TS_LATER, evidence_ref="evidence://local/config",
                  failure_reason="合成失败原因", requested='{"synthetic": true}')


def _seed_other_member(conn):
    """第二成员 cox-arch（codex / host-default / 未启用）：不进入目标成员详情。"""
    _insert_env(conn, env_id="env-2", rev=1, host_type="codex", instance_id="inst-cx-1",
                host_version=None, adapter_type="codex", connector="conn-3", digest=DIGEST_C)
    _insert_model(conn, mp_id="mp-3", mp_rev=1, env_id="env-2", env_rev=1,
                  strategy="host-default", provider=None, model_id=None, digest=DIGEST_C)
    _seed_cycle(
        conn,
        {"agent_id": OTHER, "alias": "CoX", "normalized": "cox", "display": "成员B",
         "roles": '["sales-architect"]', "capabilities": '["review","document"]',
         "env_id": "env-2", "mp_id": "mp-3", "enabled": 0},
        {"agent_id": OTHER, "binding_rev": 1, "env_id": "env-2", "env_rev": 1,
         "adapter_type": "codex", "connector": "conn-3", "execution_profile_ref": "profile-cox",
         "host_agent_id": None, "digest": DIGEST_C},
    )
    _insert_project_agent(conn, project_id="proj-c", agent_id=OTHER, roles='["sales-architect"]')
    _insert_check(conn, check_id="chk-cox", agent_id=OTHER, binding_rev=1, env_id="env-2",
                  env_rev=1, digest=DIGEST_C, mp_id="mp-3", mp_rev=1, check_type="runtime",
                  status="unverified", evidence_level="OFFICIAL", observed_at=TS,
                  evidence_ref="evidence://official/cox", requested='{}')


def _call(conn, registry_id=REGISTRY_ID, agent_id=TARGET):
    """调用并返回 (code, details)；非 ContractError 直接抛 AssertionError。

    未显式传参时补默认 REGISTRY_ID / TARGET（服务签名是 keyword-only，缺参会先
    撞 TypeError 而不是受控拒绝）；显式传入的 registry_id / agent_id 原样覆盖
    默认值 —— 包括 BAD_ARGUMENTS 里的 None，也不被默认逻辑吞掉。
    """
    try:
        inspect_registry_agent(conn, registry_id=registry_id, agent_id=agent_id)
    except ContractError as exc:
        return exc.code, exc.details
    raise AssertionError("期望受控 ContractError，实际成功返回")


def _sql_action(name, fallback):
    return getattr(sqlite3, name, fallback)


_SELECT_ACTION = _sql_action("SQLITE_SELECT", 21)
_READ_ACTION = _sql_action("SQLITE_READ", 20)
_FUNCTION_ACTION = _sql_action("SQLITE_FUNCTION", 31)
_TRANSACTION_ACTION = _sql_action("SQLITE_TRANSACTION", 22)
_SAVEPOINT_ACTION = _sql_action("SQLITE_SAVEPOINT", 32)
_RECURSIVE_ACTION = _sql_action("SQLITE_RECURSIVE", 33)

_WRITE_ACTIONS = frozenset(
    _sql_action(name, fallback)
    for name, fallback in (
        ("SQLITE_INSERT", 18), ("SQLITE_UPDATE", 23), ("SQLITE_DELETE", 9),
        ("SQLITE_CREATE_TABLE", 1), ("SQLITE_DROP_TABLE", 2),
        ("SQLITE_CREATE_INDEX", 3), ("SQLITE_DROP_INDEX", 4),
        ("SQLITE_CREATE_TRIGGER", 5), ("SQLITE_DROP_TRIGGER", 6),
        ("SQLITE_CREATE_TEMP_TABLE", 7), ("SQLITE_DROP_TEMP_TABLE", 8),
        ("SQLITE_CREATE_TEMP_TRIGGER", 10), ("SQLITE_DROP_TEMP_TRIGGER", 11),
        ("SQLITE_CREATE_TEMP_INDEX", 12), ("SQLITE_DROP_TEMP_INDEX", 13),
        ("SQLITE_CREATE_VTABLE", 29), ("SQLITE_DROP_VTABLE", 30),
        ("SQLITE_ATTACH", 24), ("SQLITE_DETACH", 25), ("SQLITE_REINDEX", 26),
        ("SQLITE_ALTER_TABLE", 15), ("SQLITE_ANALYZE", 28),
    )
)


def _read_only_authorizer(action, arg1, arg2, db, source):
    """只允许 SELECT / 事务控制 / 只读 PRAGMA；任何写动作显式 DENY。"""
    if action in _WRITE_ACTIONS:
        return sqlite3.SQLITE_DENY
    if action in (_SELECT_ACTION, _READ_ACTION, _FUNCTION_ACTION, _TRANSACTION_ACTION,
                  _SAVEPOINT_ACTION, _RECURSIVE_ACTION):
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_OK  # 只读 PRAGMA 等维持 OK


class _SnapshotSpyConnection(sqlite3.Connection):
    """可控 sqlite3.Connection 子类：只包一层 execute，用于注入 writer 提交点。

    原实现 ``mock.patch.object(sqlite3.Connection, "execute", spy)`` 实际不可执行：
    sqlite3.Connection 是 CPython 内置 / 扩展类型，不能设属性（TypeError: can't
    set attributes of built-in/extension type）。这里改由 ``connect(factory=...)``
    产出子类实例，等价且可控：

    - 服务侧 ``isinstance(conn, sqlite3.Connection)`` 判定与真实连接完全一致；
    - 除 execute 外包一层外，其余全部走真实 sqlite3 语义，不做 stub 替身；
    - 注入点仍是「事务内第一次 SELECT 返回之后」——读快照已建立之后，
      因此「同一快照 + 下一次读取才可见」的原语义被完整保留。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._after_first_select = None

    def execute(self, sql, *params):
        outcome = super().execute(sql, *params)
        hook = self._after_first_select
        if hook is not None and isinstance(sql, str) and sql.strip().upper().startswith("SELECT"):
            self._after_first_select = None  # 只注入一次
            hook()
        return outcome


class RegistryDetailTestCase(unittest.TestCase):
    """默认 fixture：legacy 库 + candidate 迁移 + 两成员（含多版本历史）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(runtime_root()))
        self.path = Path(self.tmp.name) / "candidate.sqlite3"
        self.store = _make_legacy(self.path)
        self.conn = self.store.conn
        apply_registry_candidate_migration(
            self.conn, registry_id=REGISTRY_ID, source_digest=SOURCE_DIGEST
        )
        _seed_target_member(self.conn)
        _seed_other_member(self.conn)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def detail(self, **kwargs):
        kwargs.setdefault("registry_id", REGISTRY_ID)
        kwargs.setdefault("agent_id", TARGET)
        return inspect_registry_agent(self.conn, **kwargs)


class PublicApiPositiveTests(RegistryDetailTestCase):
    """§4 公开 API 正例。"""

    def test_root_keys_and_constants(self):
        result = self.detail()
        self.assertEqual(list(result), ROOT_KEYS)
        self.assertEqual(result["schemaVersion"], "registry-agent-detail.v1")
        self.assertFalse(result["wouldWrite"])
        self.assertFalse(result["wouldDispatch"])
        self.assertTrue(result["authorityUnchanged"])
        self.assertEqual(
            list(result["registry"]),
            ["registryId", "schemaVersion", "authority", "revision", "sourceDigest"],
        )
        self.assertEqual(result["registry"]["registryId"], REGISTRY_ID)
        self.assertEqual(result["registry"]["schemaVersion"], 1)
        self.assertEqual(result["registry"]["authority"], "candidate")
        self.assertEqual(result["registry"]["revision"], 1)
        self.assertEqual(result["registry"]["sourceDigest"], SOURCE_DIGEST)

    def test_item_is_full_current_projection(self):
        item = self.detail()["item"]
        self.assertEqual(list(item), ITEM_KEYS)
        self.assertEqual(item["agentId"], TARGET)
        self.assertEqual(list(item["environment"]), ENVIRONMENT_KEYS)
        self.assertEqual(list(item["binding"]), BINDING_KEYS)
        self.assertEqual(list(item["modelProfile"]), MODEL_KEYS)
        self.assertEqual(list(item["projects"][0]), PROJECT_KEYS)
        self.assertEqual(list(item["checks"][0]), CHECK_KEYS)
        # 当前投影指向最新环境 / 绑定 / 模型版本。
        self.assertEqual(item["environment"]["revision"], 2)
        self.assertEqual(item["binding"]["bindingRevision"], 2)
        self.assertEqual(item["modelProfile"]["modelProfileId"], "mp-2")
        # 项目按 projectId 码点升序；checks 按 observedAt 降序。
        self.assertEqual([p["projectId"] for p in item["projects"]], ["proj-a", "proj-b"])
        self.assertEqual(
            [c["checkId"] for c in item["checks"]], ["chk-current", "chk-nomodel", "chk-old"]
        )
        # 不因 enabled / passed 提升 online / ready。
        for key in ("online", "ready", "status", "verified", "hostVerified"):
            self.assertNotIn(key, item)

    def test_binding_history_keeps_all_versions_in_order(self):
        result = self.detail()
        history = result["bindingHistory"]
        self.assertEqual([b["bindingRevision"] for b in history], [1, 2])
        for binding in history:
            self.assertEqual(list(binding), BINDING_HISTORY_KEYS)
        # 旧版本不被新版本覆盖：rev1 仍指向 env-1 rev1 / conn-1。
        self.assertEqual((history[0]["environmentId"], history[0]["environmentRevision"]),
                         ("env-1", 1))
        self.assertEqual(history[0]["connectorConfigRef"], "conn-1")
        self.assertEqual((history[1]["environmentId"], history[1]["environmentRevision"]),
                         ("env-1", 2))
        self.assertEqual(history[1]["connectorConfigRef"], "conn-2")
        self.assertEqual(history[0]["createdAt"], TS_OLD)

    def test_environment_history_covers_all_referenced_versions(self):
        result = self.detail()
        history = result["environmentHistory"]
        self.assertEqual([(e["environmentId"], e["revision"]) for e in history],
                         [("env-1", 1), ("env-1", 2)])
        for environment in history:
            self.assertEqual(list(environment), ENVIRONMENT_KEYS)
        # 不泄露 internal 记账 / 权限 JSON / 凭据相关列。
        for environment in history:
            for key in ("createdByJson", "allowedRootsJson", "permissionCeilingJson",
                        "created_by_json", "connectorSecret"):
                self.assertNotIn(key, environment)

    def test_model_profiles_covers_history_and_host_default_is_kept(self):
        result = self.detail()
        profiles = result["modelProfiles"]
        self.assertEqual([(m["modelProfileId"], m["profileRevision"]) for m in profiles],
                         [("mp-1", 1), ("mp-2", 1)])
        for profile in profiles:
            self.assertEqual(list(profile), MODEL_KEYS)
        self.assertEqual(profiles[0]["modelId"], "synthetic-model-1")
        # 第二成员的 host-default 不出现在目标成员详情里。
        self.assertNotIn("mp-3", [m["modelProfileId"] for m in profiles])

    def test_other_member_has_own_history_and_host_default_nulls(self):
        result = self.detail(agent_id=OTHER)
        item = result["item"]
        self.assertEqual(item["agentId"], OTHER)
        self.assertFalse(item["enabled"])
        self.assertEqual(item["modelProfile"]["strategy"], "host-default")
        self.assertIsNone(item["modelProfile"]["provider"])
        self.assertIsNone(item["modelProfile"]["modelId"])
        self.assertEqual([b["bindingRevision"] for b in result["bindingHistory"]], [1])
        self.assertEqual([(e["environmentId"], e["revision"]) for e in result["environmentHistory"]],
                         [("env-2", 1)])
        self.assertEqual([m["modelProfileId"] for m in result["modelProfiles"]], ["mp-3"])
        self.assertEqual([p["projectId"] for p in item["projects"]], ["proj-c"])

    def test_synthetic_and_expired_checks_are_faithful(self):
        checks = {c["checkId"]: c for c in self.detail()["item"]["checks"]}
        self.assertEqual(checks["chk-old"]["evidenceLevel"], "SYNTHETIC")
        self.assertEqual(checks["chk-old"]["status"], "passed")
        self.assertEqual(checks["chk-old"]["expiresAt"], TS_EXPIRED)
        self.assertEqual(checks["chk-old"]["observed"], {"note": "old"})
        self.assertIsNone(checks["chk-nomodel"]["observed"])
        self.assertIsNone(checks["chk-nomodel"]["modelProfileId"])

    def test_tuple_and_row_row_factory_agree(self):
        tuple_result = self.detail()
        try:
            self.conn.row_factory = sqlite3.Row
            row_result = self.detail()
        finally:
            self.conn.row_factory = None
        self.assertEqual(row_result, tuple_result)

    def test_second_call_is_identical_and_json_serializable(self):
        first = self.detail()
        second = self.detail()
        self.assertEqual(first, second)
        json.dumps(first, sort_keys=True, allow_nan=False)

    def test_projects_and_checks_stay_inside_item_only(self):
        result = self.detail()
        # 不在顶层另建第二份可写状态。
        for key in ("projects", "checks", "binding", "environment", "modelProfile"):
            self.assertNotIn(key, result)
        self.assertEqual(len(result["item"]["projects"]), 2)
        self.assertEqual(len(result["item"]["checks"]), 3)


class ArgumentAndStateNegativeTests(RegistryDetailTestCase):
    """§4 参数 / 状态负例。"""

    def test_missing_keyword_only_signature(self):
        with self.assertRaises(TypeError):
            inspect_registry_agent(self.conn, REGISTRY_ID, TARGET)
        with self.assertRaises(TypeError):
            inspect_registry_agent(self.conn, registry_id=REGISTRY_ID)

    def test_bad_registry_id_and_agent_id(self):
        for bad in BAD_ARGUMENTS:
            with self.subTest(registry_id=repr(bad)):
                code, details = _call(self.conn, registry_id=bad, agent_id=TARGET)
                self.assertEqual((code, details), ("invalid_task",
                                                   {"field": "registryId", "reason": "invalid_argument"}))
            with self.subTest(agent_id=repr(bad)):
                code, details = _call(self.conn, registry_id=REGISTRY_ID, agent_id=bad)
                self.assertEqual((code, details), ("invalid_task",
                                                   {"field": "agentId", "reason": "invalid_argument"}))

    def test_missing_member_is_stable_rejection(self):
        code, details = _call(self.conn, agent_id="solo-1")
        self.assertEqual((code, details), ("invalid_task",
                                           {"field": "agentId", "reason": "missing_reference"}))

    def test_unmigrated_library(self):
        other = Path(self.tmp.name) / "plain.sqlite3"
        store = SqliteStore(other)
        try:
            code, details = _call(store.conn)
            self.assertEqual((code, details), ("invalid_task",
                                               {"field": "registry", "reason": "registry_unmigrated"}))
        finally:
            store.close()

    def test_wrong_registry_id(self):
        with self.assertRaises(ContractError) as ctx:
            inspect_registry_agent(self.conn, registry_id=OTHER_REGISTRY_ID, agent_id=TARGET)
        self.assertEqual(ctx.exception.code, "invalid_task")
        self.assertEqual(ctx.exception.details["field"], "registryId")

    def test_caller_transaction_is_rejected_and_kept(self):
        self.conn.execute("BEGIN")
        try:
            code, details = _call(self.conn)
            self.assertEqual((code, details), ("invalid_task",
                                               {"field": "connection", "reason": "connection_in_transaction"}))
            self.assertTrue(self.conn.in_transaction)
        finally:
            self.conn.execute("ROLLBACK")
        self.assertFalse(self.conn.in_transaction)

    def test_unsupported_row_factory_is_not_mutated(self):
        def foreign(cursor, row):
            return {"x": row}

        self.conn.row_factory = foreign
        try:
            code, details = _call(self.conn)
            self.assertEqual((code, details), ("invalid_task",
                                               {"field": "connection", "reason": "unsupported_row_factory"}))
            self.assertIs(self.conn.row_factory, foreign)
        finally:
            self.conn.row_factory = None

    def test_non_connection_is_rejected(self):
        for bad in (None, "conn", 42, object()):
            with self.subTest(conn=repr(bad)):
                code, details = _call(bad)
                self.assertEqual((code, details), ("invalid_task",
                                                   {"field": "connection", "reason": "connection_required"}))

    def test_closed_connection_keeps_sqlite_semantics(self):
        extra = sqlite3.connect(str(self.path))
        extra.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            inspect_registry_agent(extra, registry_id=REGISTRY_ID, agent_id=TARGET)

    def test_authorizer_denial_is_not_wrapped(self):
        def deny_agents(action, arg1, arg2, db, source):
            if action == _READ_ACTION and arg1 == "agent_checks":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        self.conn.set_authorizer(deny_agents)
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                inspect_registry_agent(self.conn, registry_id=REGISTRY_ID, agent_id=TARGET)
        finally:
            self.conn.set_authorizer(None)


class ReferenceIntegrityTests(RegistryDetailTestCase):
    """§4 引用完整性：目标成员的损坏不得被隐藏。"""

    def test_other_member_damage_does_not_break_target_detail(self):
        """外泄原则：目标成员之外的数据不进详情，也不阻断目标成员。"""
        _raw(self.conn,
             "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id,"
             " environment_revision, adapter_type, connector_config_ref, execution_profile_ref,"
             " host_agent_id, config_digest, created_by_json, created_at)"
             " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             (OTHER, 2, "env-9", 9, "codex", "conn-3", None, "host-x", DIGEST_C, "{}", TS))
        result = self.detail()
        self.assertEqual(result["item"]["agentId"], TARGET)
        self.assertEqual([b["bindingRevision"] for b in result["bindingHistory"]], [1, 2])

    def test_history_binding_missing_environment(self):
        _raw(self.conn,
             "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id,"
             " environment_revision, adapter_type, connector_config_ref, execution_profile_ref,"
             " host_agent_id, config_digest, created_by_json, created_at)"
             " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             (TARGET, 3, "env-9", 9, "workbuddy", "conn-2", None, "host-wkb1", DIGEST_B, "{}", TS))
        code, details = _call(self.conn)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertTrue(details["field"].startswith("agents.%s.environmentHistory." % TARGET))

    def test_history_binding_adapter_mismatch(self):
        _raw(self.conn,
             "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id,"
             " environment_revision, adapter_type, connector_config_ref, execution_profile_ref,"
             " host_agent_id, config_digest, created_by_json, created_at)"
             " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
             (TARGET, 3, "env-1", 2, "codex", "conn-2", None, "host-wkb1", DIGEST_B, "{}", TS))
        code, details = _call(self.conn)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "reference_mismatch")
        self.assertTrue(details["field"].startswith("agents.%s." % TARGET))

    def test_check_missing_binding(self):
        _raw(self.conn,
             "INSERT INTO agent_checks (check_id, agent_id, binding_revision, environment_id,"
             " environment_revision, config_digest, model_profile_id, model_profile_revision,"
             " check_type, status, evidence_level, observed_at, expires_at, evidence_ref,"
             " failure_reason, requested_json, observed_json, created_by_json)"
             " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("chk-orphan", TARGET, 99, "env-1", 2, DIGEST_B, None, None, "config", "failed",
              "LOCAL", TS, TS_LATER, "evidence://synthetic/orphan", None, "{}", None, "{}"))
        code, details = _call(self.conn)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertIn("agents.%s." % TARGET, details["field"])

    def test_check_missing_model_profile(self):
        _raw(self.conn,
             "INSERT INTO agent_checks (check_id, agent_id, binding_revision, environment_id,"
             " environment_revision, config_digest, model_profile_id, model_profile_revision,"
             " check_type, status, evidence_level, observed_at, expires_at, evidence_ref,"
             " failure_reason, requested_json, observed_json, created_by_json)"
             " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("chk-nomp", TARGET, 2, "env-1", 2, DIGEST_B, "mp-9", 9, "model", "failed",
              "LOCAL", TS, TS_LATER, "evidence://synthetic/nomp", None, "{}", None, "{}"))
        code, details = _call(self.conn)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertIn("modelProfile", details["field"])

    def test_check_model_environment_revision_mismatch(self):
        """check 引用 mp-1 rev1，但环境版本写成 rev2 → 版本错配必须暴露。"""
        _raw(self.conn,
             "INSERT INTO agent_checks (check_id, agent_id, binding_revision, environment_id,"
             " environment_revision, config_digest, model_profile_id, model_profile_revision,"
             " check_type, status, evidence_level, observed_at, expires_at, evidence_ref,"
             " failure_reason, requested_json, observed_json, created_by_json)"
             " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
             ("chk-shift", TARGET, 2, "env-1", 2, DIGEST_B, "mp-1", 1, "model", "failed",
              "LOCAL", TS, TS_LATER, "evidence://synthetic/shift", None, "{}", None, "{}"))
        self.assertEqual(_call(self.conn)[1]["reason"], "reference_mismatch")

    def test_project_broken_json_is_exposed(self):
        self.conn.execute(
            "UPDATE project_agents SET project_roles_json = ? WHERE agent_id = ? AND project_id = ?",
            ('["skill-worker"', TARGET, "proj-a"),
        )
        self.assertEqual(_call(self.conn)[1]["reason"], "invalid_persisted_value")

    def test_member_broken_json_is_exposed(self):
        self.conn.execute(
            "UPDATE registry_agents SET roles_json = ? WHERE agent_id = ?",
            ('{"not": "a list"}', TARGET),
        )
        self.assertEqual(_call(self.conn)[1]["reason"], "invalid_persisted_value")


class JsonBoundaryTests(RegistryDetailTestCase):
    """§4 JSON 边界：重复键 / NaN / Infinity / 指数溢出 / 有限值保留。"""

    def _check_with_requested(self, requested, check_id="chk-json"):
        _insert_check(conn=self.conn, check_id=check_id, agent_id=TARGET, binding_rev=2,
                      env_id="env-1", env_rev=2, digest=DIGEST_B, check_type="config",
                      status="unverified", evidence_level="LOCAL", observed_at=TS,
                      requested=requested)

    def test_duplicate_key_rejected(self):
        self._check_with_requested('{"a": 1, "a": 2}')
        self.assertEqual(_call(self.conn)[1]["reason"], "invalid_persisted_value")

    def test_nan_and_infinity_rejected(self):
        # 历史表有 immutable trigger，负例数据只追加不删除：每个 payload 用独立 check_id。
        for index, payload in enumerate(('{"r": NaN}', '{"r": Infinity}', '{"r": -Infinity}')):
            with self.subTest(payload=payload):
                self._check_with_requested(payload, check_id="chk-nan-%d" % index)
                self.assertEqual(_call(self.conn)[1]["reason"], "invalid_persisted_value")

    def test_exponent_overflow_rejected(self):
        for index, payload in enumerate(('{"r": 1e999}', '{"r": -1e999}')):
            with self.subTest(payload=payload):
                self._check_with_requested(payload, check_id="chk-exp-%d" % index)
                self.assertEqual(_call(self.conn)[1]["reason"], "invalid_persisted_value")

    def test_nested_non_finite_rejected(self):
        self._check_with_requested('{"a": {"b": [1, 2, 1e999]}}')
        self.assertEqual(_call(self.conn)[1]["reason"], "invalid_persisted_value")

    def test_finite_and_string_values_preserved(self):
        self._check_with_requested(
            '{"big": 1e308, "neg": -1.5, "s1": "Infinity", "s2": "1e999", "i": 7, "n": null}'
        )
        checks = {c["checkId"]: c for c in self.detail()["item"]["checks"]}
        payload = checks["chk-json"]["requested"]
        self.assertEqual(payload["big"], 1e308)
        self.assertEqual(payload["neg"], -1.5)
        self.assertEqual(payload["s1"], "Infinity")
        self.assertEqual(payload["s2"], "1e999")
        self.assertEqual(payload["i"], 7)
        self.assertIsNone(payload["n"])
        json.dumps(payload, allow_nan=False)


class ReadOnlyAndSnapshotTests(RegistryDetailTestCase):
    """§4 只读 / 快照 / 隔离。"""

    def test_no_write_statements_and_state_preserved(self):
        before = self.conn.total_changes
        row_factory = self.conn.row_factory
        dumps_before = {
            name: self.conn.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0]
            for name in ("registry_agents", "agent_bindings", "environments", "model_profiles",
                         "agent_checks", "project_agents", "registry_metadata")
        }
        self.conn.set_authorizer(_read_only_authorizer)
        try:
            result = self.detail()
        finally:
            # 本环境 sqlite3 在 set_authorizer(None) 之后，同一连接上的新 prepare 仍可能
            # 返回 not authorized；因此该连接清理后不再发起任何查询，后续可读 / 状态复核
            # 改由独立只读验证连接读同一个已提交的 SQLite 文件完成。
            self.conn.set_authorizer(None)
        self.assertEqual(result["item"]["agentId"], TARGET)
        self.assertEqual(self.conn.total_changes, before)
        self.assertIs(self.conn.row_factory, row_factory)
        self.assertFalse(self.conn.in_transaction)
        # 状态未变化 / 后续可读：独立验证连接只做 SELECT，且 fixture 全部已提交
        # （SqliteStore 用 isolation_level=None 自动提交；_seed_cycle 与迁移器显式
        # COMMIT），所以独立连接与 fixture 连接读到同一份已提交数据。
        verifier = sqlite3.connect(str(self.path))
        try:
            verifier.execute("PRAGMA query_only = ON")
            for name, count in dumps_before.items():
                self.assertEqual(
                    verifier.execute("SELECT COUNT(*) FROM %s" % name).fetchone()[0],
                    count, "%s 行数被改动" % name,
                )
        finally:
            verifier.close()

    def test_snapshot_is_stable_when_writer_commits(self):
        """详情事务开启后 writer 提交更新：本次返回必须仍是同一快照，下次调用才可见。"""
        writer_done = {"count": 0}

        def _writer_commit():
            writer = sqlite3.connect(str(self.path))
            try:
                writer.execute("UPDATE registry_agents SET display_name = ? WHERE agent_id = ?",
                               ("成员A-改名", TARGET))
                writer.commit()
            finally:
                writer.close()

        def on_first_select():
            # deferred 事务的读快照真正建立在第一次 SELECT（拿到 SHARED 锁）之后，
            # 因此注入点放在首次 SELECT 之后，才能确定性证明「事务内同一快照」。
            writer_done["count"] += 1
            _writer_commit()

        conn = sqlite3.connect(str(self.path), factory=_SnapshotSpyConnection)
        try:
            # 与 fixture 连接一致：关闭隐式事务，由服务显式 BEGIN / COMMIT 控制。
            conn.isolation_level = None
            conn._after_first_select = on_first_select
            result = inspect_registry_agent(conn, registry_id=REGISTRY_ID, agent_id=TARGET)
        finally:
            conn.close()
        self.assertEqual(result["item"]["displayName"], "成员A")
        self.assertEqual(writer_done["count"], 1)
        # 下一次调用看到新值。
        self.assertEqual(self.detail()["item"]["displayName"], "成员A-改名")

    def test_no_open_or_network_or_subprocess_access(self):
        real_connect = sqlite3.connect
        real_open = open
        with mock.patch("sqlite3.connect", side_effect=AssertionError("不得 connect")), \
                mock.patch("builtins.open", side_effect=AssertionError("不得 open")), \
                mock.patch("socket.socket", side_effect=AssertionError("不得建 socket")), \
                mock.patch("subprocess.Popen", side_effect=AssertionError("不得起子进程")), \
                mock.patch("os.getenv", side_effect=AssertionError("不得读 env")):
            result = self.detail()
        self.assertEqual(result["item"]["agentId"], TARGET)
        self.assertIs(real_connect, sqlite3.connect)
        self.assertIs(real_open, open)

    def test_read_only_mode_connection_succeeds(self):
        uri = "file:%s?mode=ro" % self.path
        reader = sqlite3.connect(uri, uri=True)
        try:
            result = inspect_registry_agent(reader, registry_id=REGISTRY_ID, agent_id=TARGET)
            self.assertEqual(result["item"]["agentId"], TARGET)
        finally:
            reader.close()

    def test_only_one_public_service_entry(self):
        """服务入口唯一：本模块只新定义一个可调用 API，不给 CLI 预留第二套接口。"""
        import service.registry_detail as detail_module

        own_callables = [
            name
            for name in dir(detail_module)
            if not name.startswith("_")
            and callable(getattr(detail_module, name))
            and getattr(getattr(detail_module, name), "__module__", None) == detail_module.__name__
        ]
        self.assertEqual(own_callables, ["inspect_registry_agent"])


class IsolationAndRegressionTests(RegistryDetailTestCase):
    """§4 范围与回归边界。"""

    def test_list_service_signature_untouched(self):
        """详情服务 import 列表服务只读，不得改变列表服务公开 API。"""
        import service.registry_read as read_module

        self.assertTrue(callable(read_module.list_registry_agents))
        self.assertIsInstance(read_module.READ_SCHEMA_VERSION, str)
        self.assertEqual(read_module.READ_SCHEMA_VERSION, "registry-agent-list.v1")

    def test_module_file_is_only_new_product_file(self):
        import service.registry_detail as detail_module

        self.assertTrue(detail_module.__file__.endswith("service/registry_detail.py"))

    def test_runtime_root_is_outside_workspace_tree(self):
        """Fixture 只落在本轮隔离 runtime 下，不碰工作区 / 历史 fixture 目录。"""
        self.assertTrue(str(self.path).startswith(str(runtime_root())), str(self.path))
        self.assertNotIn("/Users/demo/.hermes/skills/conductor/", str(self.path))
        self.assertEqual(self.path.name, "candidate.sqlite3")


# ---------------------------------------------------------------------------
# 本轮补齐：空历史正例 + 当前引用缺失负例 + schema 漂移负例
# ---------------------------------------------------------------------------
class EmptyHistoryAndOrderingTests(RegistryDetailTestCase):
    """§4 补齐正例：空历史返回 []、历史排序与去重不依赖插入顺序。"""

    def test_empty_history_lists_are_returned_not_invented(self):
        """无 project / 无 check 的成员：三段历史只含自身当前引用，其余为 []。"""
        _insert_legacy_agent(self.conn, "solo-2", "成员D")
        _insert_env(conn=self.conn, env_id="env-3", rev=1, host_type="codebuddy",
                    instance_id="inst-solo-2", host_version=None, adapter_type="codebuddy",
                    connector="conn-4", digest=DIGEST_C)
        _insert_model(conn=self.conn, mp_id="mp-4", mp_rev=1, env_id="env-3", env_rev=1,
                      strategy="fixed", provider="synthetic-provider",
                      model_id="synthetic-model-4", digest=DIGEST_C)
        _seed_cycle(
            self.conn,
            {"agent_id": "solo-2", "alias": "Solo2", "normalized": "solo2", "display": "成员D",
             "env_id": "env-3", "env_rev": 1, "mp_id": "mp-4", "mp_rev": 1,
             "binding_rev": 1, "enabled": 1},
            {"agent_id": "solo-2", "binding_rev": 1, "env_id": "env-3", "env_rev": 1,
             "adapter_type": "codebuddy", "connector": "conn-4", "host_agent_id": "host-solo-2",
             "digest": DIGEST_C, "created_at": TS},
        )

        result = inspect_registry_agent(self.conn, registry_id=REGISTRY_ID, agent_id="solo-2")
        item = result["item"]
        self.assertEqual(item["agentId"], "solo-2")
        # 空历史必须显式返回 []，不是 None，也不是别处补出的第二份状态。
        self.assertEqual(item["projects"], [])
        self.assertEqual(item["checks"], [])
        self.assertEqual([b["bindingRevision"] for b in result["bindingHistory"]], [1])
        self.assertEqual(list(result["bindingHistory"][0]), BINDING_HISTORY_KEYS)
        # 只含自身引用的精确版本：其他成员的 env-1 / env-2 / mp-3 不得出现。
        self.assertEqual(
            [(e["environmentId"], e["revision"]) for e in result["environmentHistory"]],
            [("env-3", 1)],
        )
        self.assertEqual(
            [(m["modelProfileId"], m["profileRevision"]) for m in result["modelProfiles"]],
            [("mp-4", 1)],
        )
        json.dumps(result, sort_keys=True, allow_nan=False)

    def test_binding_history_sorted_and_env_history_deduped(self):
        """逆序插入三个 binding：历史仍按 revision 升序，环境版本去重后稳定排序。"""
        _insert_legacy_agent(self.conn, "solo-3", "成员E")
        _insert_env(conn=self.conn, env_id="env-4", rev=1, host_type="codebuddy",
                    instance_id="inst-solo-3", adapter_type="codebuddy", connector="conn-4",
                    digest=DIGEST_C)
        _insert_env(conn=self.conn, env_id="env-4", rev=2, host_type="codebuddy",
                    instance_id="inst-solo-3b", adapter_type="codebuddy", connector="conn-4",
                    digest=DIGEST_C)
        _insert_env(conn=self.conn, env_id="env-5", rev=1, host_type="codebuddy",
                    instance_id="inst-solo-3c", adapter_type="codebuddy", connector="conn-5",
                    digest=DIGEST_C)
        _insert_model(conn=self.conn, mp_id="mp-5", mp_rev=1, env_id="env-4", env_rev=2,
                      strategy="fixed", provider="synthetic-provider",
                      model_id="synthetic-model-5", digest=DIGEST_C)

        _seed_cycle(
            self.conn,
            {"agent_id": "solo-3", "alias": "Solo3", "normalized": "solo3", "display": "成员E",
             "env_id": "env-4", "env_rev": 2, "mp_id": "mp-5", "mp_rev": 1,
             "binding_rev": 3, "enabled": 1},
            # 故意逆序插入：先 rev3，再 rev1，最后 rev2。
            {"agent_id": "solo-3", "binding_rev": 3, "env_id": "env-4", "env_rev": 2,
             "adapter_type": "codebuddy", "connector": "conn-4", "host_agent_id": "host-solo-3",
             "digest": DIGEST_C, "created_at": TS_NEWEST},
            {"agent_id": "solo-3", "binding_rev": 1, "env_id": "env-4", "env_rev": 1,
             "adapter_type": "codebuddy", "connector": "conn-4", "host_agent_id": "host-solo-3",
             "digest": DIGEST_C, "created_at": TS_OLD},
            {"agent_id": "solo-3", "binding_rev": 2, "env_id": "env-5", "env_rev": 1,
             "adapter_type": "codebuddy", "connector": "conn-5", "host_agent_id": "host-solo-3",
             "digest": DIGEST_C, "created_at": TS},
        )

        result = inspect_registry_agent(self.conn, registry_id=REGISTRY_ID, agent_id="solo-3")
        history = result["bindingHistory"]
        self.assertEqual([b["bindingRevision"] for b in history], [1, 2, 3])
        self.assertEqual(
            [(b["environmentId"], b["environmentRevision"]) for b in history],
            [("env-4", 1), ("env-5", 1), ("env-4", 2)],
        )
        self.assertEqual([b["createdAt"] for b in history], [TS_OLD, TS, TS_NEWEST])
        self.assertEqual(
            [(e["environmentId"], e["revision"]) for e in result["environmentHistory"]],
            [("env-4", 1), ("env-4", 2), ("env-5", 1)],
        )
        self.assertEqual(
            [(m["modelProfileId"], m["profileRevision"]) for m in result["modelProfiles"]],
            [("mp-5", 1)],
        )
        self.assertEqual(result["item"]["projects"], [])
        self.assertEqual(result["item"]["checks"], [])


class MissingCurrentReferenceTests(RegistryDetailTestCase):
    """§4 补齐负例：当前环境 / 当前 binding / 当前 model 缺失都必须 fail closed。"""

    def test_current_environment_missing_is_rejected(self):
        _insert_legacy_agent(self.conn, "ghost-1", "成员F")
        _seed_member_with_fk_off(
            self.conn,
            {"agent_id": "ghost-1", "alias": "Ghost1", "normalized": "ghost1",
             "display": "成员F", "env_id": "env-7", "env_rev": 1, "mp_id": "mp-1",
             "mp_rev": 1, "binding_rev": 1, "enabled": 1},
        )
        code, details = _call(self.conn, agent_id="ghost-1")
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details, {"field": "agents.ghost-1.environment",
                                  "reason": "missing_reference"})

    def test_current_binding_missing_is_rejected(self):
        _insert_legacy_agent(self.conn, "ghost-2", "成员G")
        # 当前环境与当前模型都存在，只有 currentBindingRevision 指向不存在的版本。
        _seed_member_with_fk_off(
            self.conn,
            {"agent_id": "ghost-2", "alias": "Ghost2", "normalized": "ghost2",
             "display": "成员G", "env_id": "env-1", "env_rev": 1, "mp_id": "mp-1",
             "mp_rev": 1, "binding_rev": 5, "enabled": 1},
        )
        code, details = _call(self.conn, agent_id="ghost-2")
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details, {"field": "agents.ghost-2.binding",
                                  "reason": "missing_reference"})

    def test_current_model_profile_missing_is_rejected(self):
        _insert_legacy_agent(self.conn, "ghost-3", "成员H")
        # 当前环境与当前 binding 都存在，只有当前 model profile 版本不存在。
        _seed_member_with_fk_off(
            self.conn,
            {"agent_id": "ghost-3", "alias": "Ghost3", "normalized": "ghost3",
             "display": "成员H", "env_id": "env-1", "env_rev": 1, "mp_id": "mp-9",
             "mp_rev": 1, "binding_rev": 1, "enabled": 1},
            {"agent_id": "ghost-3", "binding_rev": 1, "env_id": "env-1", "env_rev": 1,
             "adapter_type": "workbuddy", "connector": "conn-1",
             "host_agent_id": "host-ghost-3", "digest": DIGEST_A, "created_at": TS},
        )
        code, details = _call(self.conn, agent_id="ghost-3")
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details, {"field": "agents.ghost-3.modelProfile",
                                  "reason": "missing_reference"})


class SchemaDriftAndObservedJsonTests(RegistryDetailTestCase):
    """§4 补齐负例：迁移形态漂移、``observed_json`` 非有限值。"""

    def test_missing_trigger_is_rejected(self):
        self.conn.execute("DROP TRIGGER registry_agent_checks_no_delete")
        code, details = _call(self.conn)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details, {"field": "registry_agent_checks_no_delete",
                                  "reason": "missing_trigger"})

    def test_relaxed_same_name_trigger_is_rejected(self):
        """同名 trigger 被换成放宽 body：不能因为名字在就放行（schema 漂移）。"""
        self.conn.execute("DROP TRIGGER registry_environments_no_update")
        self.conn.execute(
            "CREATE TRIGGER registry_environments_no_update BEFORE UPDATE ON environments "
            "BEGIN SELECT 1; END"
        )
        code, details = _call(self.conn)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details, {"field": "registry_environments_no_update",
                                  "reason": "trigger_definition_mismatch"})

    def test_non_finite_observed_json_is_rejected(self):
        for index, payload in enumerate(('{"r": NaN}', '{"r": 1e999}')):
            with self.subTest(observed=payload):
                _insert_check(conn=self.conn, check_id="chk-obs-%d" % index, agent_id=TARGET,
                              binding_rev=2, env_id="env-1", env_rev=2, digest=DIGEST_B,
                              check_type="config", status="passed", evidence_level="LOCAL",
                              observed_at=TS, requested='{}', observed=payload)
                self.assertEqual(_call(self.conn)[1]["reason"], "invalid_persisted_value")


if __name__ == "__main__":
    unittest.main()
