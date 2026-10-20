"""D4-DSH-MEMBER-MODEL-PREFLIGHT-R1 / R2 定向测试：离线派发前成员 / 模型只读预检。

运行（工作目录 shared/agent-orchestrator）::

    python3 -m unittest discover -s tests -p test_d4_dsh_member_preflight.py

范围：
- 只新增本测试文件与 service/dsh_member_preflight.py；不改 registry_read / d4ctl /
  Adapter / 数据库迁移 / 合同 / 宿主配置 / 文档 / 正式库。
- 只使用**内存** SQLite 库（``SqliteStore(":memory:")`` + 既有候选迁移器生成），
  不打开 development.sqlite / 历史 fixture / 正式库，也不在磁盘落任何合成数据文件。
- 覆盖：allow、alias 冲突、未知 alias、disabled、未完成登记、project mismatch、
  model mismatch、stale check（旧 binding / 旧 model / 旧 configDigest）、过期 / 缺失 /
  failed / unverified / UNVERIFIED evidence、只读事务、身份门槛、payload 自报身份。
- ``agent_checks`` / ``agent_bindings`` 是不可变表（trigger 禁止 UPDATE / DELETE），
  所有场景都按“换一个成员 / 只 INSERT”构造，不依赖 UPDATE / DELETE 历史行。
- ``ArtifactSha256Tests`` 用 hashlib 打印两个交付文件的实测 SHA-256，供交付报告引用
  （本卡 bashCommands 只授权一条 unittest 命令，故核对与留证都做进测试）。

R2 追加（判定语义修正）：
- ``LatestCheckSelectionTests``：每种 check 只按绑定当前引用的最新 ``observedAt`` 记录判定
  （旧失败 + 新通过、旧通过 + 新失败、最新过期不回退旧有效、失败恢复、带时区排序、
  同刻 tie 的 checkId 次序、无效 ``observedAt`` fail closed、旧绑定记录只在无当前绑定记录时
  才归为 stale）。
- ``RequiredChecksContractTests``：完整派发预检固定四类必查，``required_checks`` 的缩减 /
  增加 / 重复 / 非法集合一律拒绝，且非法入参不执行任何查询。
- R1 的 ``test_required_checks_subset`` 与 ``test_required_checks_variants`` 中“可缩小必查
  集合并拿到 allow”的旧断言，按 R2 修正为受控拒绝；其余 R1 用例断言保持原语义。
"""
import hashlib
import json
import sqlite3
import subprocess
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from contracts.schemas import ContractError
from service.context import owner_context, reviewer_context, source_context, worker_context
from service.registry_read import list_registry_agents

import service.dsh_member_preflight as dmp
from service.dsh_member_preflight import preflight_member
from storage.registry_migrations import apply_registry_candidate_migration
from storage.sqlite_store import SqliteStore

TS = "2026-10-08T00:00:00+08:00"
TS_LATER = "2026-10-08T01:00:00+08:00"

# 预检时间由调用方显式注入（本模块不读时钟），全部用例共用同一时刻。
EVAL_AT = "2026-10-08T03:00:00+08:00"
FUTURE = "2026-10-08T04:00:00+08:00"
EXPIRED_AT = "2026-10-08T02:00:00+08:00"
NAIVE_AT = "2030-01-01T00:00:00"

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
REGISTRY_TABLES = (
    "registry_metadata", "environments", "model_profiles", "registry_agents", "agent_bindings",
    "project_agents", "agent_checks", "attempt_execution_bindings", "registry_changes",
)

TOP_KEYS = [
    "schemaVersion", "registry", "request", "caller", "decision", "reasons", "member", "project",
    "checks", "checksSatisfied", "evidence", "claims", "authority",
]
CHECK_ENTRY_KEYS = [
    "checkType", "state", "satisfied", "checkId", "status", "evidenceLevel", "observedAt",
    "expiresAt", "boundToCurrentReference",
]
EVIDENCE_KEYS = [
    "identity", "environment", "binding", "model", "runtime", "host", "project",
    "permissionCeiling",
]

REQUIRED_TYPES = ("config", "identity", "model", "runtime")

# 每个成员一种 check 场景（agent_checks 不可变，用不同成员隔离场景，不做 UPDATE / DELETE）。
MEMBER_FULL = "dev-wkb1"        # 四类 check 全有效（fixed 模型）
MEMBER_DISABLED = "cox-arch"    # 停用成员
MEMBER_HOST_DEFAULT = "solo-hd"  # host-default 模型 + 四类 check 有效
MEMBER_NO_CHECKS = "solo-none"  # 无任何 check
MEMBER_CONFIG_ONLY = "solo-cfg"  # 只有 config check
MEMBER_STALE_BINDING = "solo-bd"  # 四类 check 绑定旧 configDigest
MEMBER_STALE_MODEL = "solo-md"   # 四类 check 中 model 绑定旧 model profile

ALL_MEMBERS = (
    MEMBER_FULL, MEMBER_DISABLED, MEMBER_HOST_DEFAULT, MEMBER_NO_CHECKS,
    MEMBER_CONFIG_ONLY, MEMBER_STALE_BINDING, MEMBER_STALE_MODEL,
)


# -- fixture helper ---------------------------------------------------------
def _make_store():
    """内存库 + legacy schema + 合成旧行（供 registry FK 引用），不落磁盘。"""
    store = SqliteStore(":memory:")
    conn = store.conn
    conn.execute(
        "INSERT INTO sources (source_id, label, registered_at) VALUES ('src-1','本地来源',?)", (TS,)
    )
    for agent_id in ALL_MEMBERS:
        conn.execute(
            "INSERT INTO agents (agent_id, label, note, registered_at) VALUES (?,?,?,?)",
            (agent_id, "成员-%s" % agent_id, "synthetic registry entry", TS),
        )
    conn.execute(
        "INSERT INTO tasks (task_id, project_id, title, status, revision, created_at, updated_at)"
        " VALUES ('task-legacy-1','d4-product','旧任务','closed',1,?,?)", (TS, TS)
    )
    conn.execute(
        "INSERT INTO attempts (attempt_id, task_id, ordinal, state, created_at)"
        " VALUES ('att-legacy-1','task-legacy-1',1,'delivered',?)", (TS,)
    )
    apply_registry_candidate_migration(
        conn, registry_id=REGISTRY_ID, source_digest=SOURCE_DIGEST
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


def _insert_project_agent(conn, project_id, agent_id, roles='["skill-worker"]', enabled=1,
                          revision=1):
    conn.execute(
        "INSERT INTO project_agents (project_id, agent_id, project_roles_json,"
        " permission_ceiling_json, enabled, revision, created_by_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (project_id, agent_id, roles, CEILING_JSON, enabled, revision, "{}", TS, TS),
    )


def _insert_check(conn, check_id, agent_id, check_type, status="passed", evidence_level="LOCAL",
                  observed_at=TS, expires_at=FUTURE, digest=DIGEST_A, binding_rev=1,
                  env_id="env-1", env_rev=1, mp_id=None, mp_rev=None, failure_reason=None,
                  evidence_ref="evidence://synthetic/1", requested='{"synthetic": true}',
                  observed=None):
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


def _seed_four_checks(conn, agent_id, mp_id="mp-1", mp_rev=1, digest=DIGEST_A,
                      env_id="env-1", env_rev=1, config=None, identity=None, model=None,
                      runtime=None):
    """插四类 check（默认全部有效）；per_type 给 dict 覆写单个类型的字段。"""
    base = {"status": "passed", "evidence_level": "LOCAL", "observed_at": TS, "expires_at": FUTURE}
    overrides = {"config": config, "identity": identity, "model": model, "runtime": runtime}
    for check_type in REQUIRED_TYPES:
        kwargs = dict(base)
        kwargs.update(overrides[check_type] or {})
        if check_type == "model":
            kwargs.setdefault("mp_id", mp_id)
            kwargs.setdefault("mp_rev", mp_rev)
        _insert_check(conn, "chk-%s-%s" % (agent_id, check_type), agent_id, check_type,
                      digest=digest, env_id=env_id, env_rev=env_rev, **kwargs)


def _dump_tables(conn):
    """legacy + registry 全表逐行快照（含 sqlite_master 与 total_changes）。"""
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
        preflight_member(conn, **kwargs)
    except ContractError as exc:
        return exc.code, exc.details
    raise AssertionError("期望受控 ContractError，实际成功返回")


def _codes(result):
    return [reason["code"] for reason in result["reasons"]]


class PreflightTestCase(unittest.TestCase):
    """默认 fixture：内存候选库 + 七个按场景隔离的合成成员。"""

    def setUp(self):
        self.store = _make_store()
        self.conn = self.store.conn
        self._seed()

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass

    def _seed(self):
        conn = self.conn
        _insert_env(conn, env_id="env-1", rev=1, host_type="workbuddy", instance_id="inst-wkb-1",
                    adapter_type="workbuddy", connector="conn-1", digest=DIGEST_A)
        _insert_env(conn, env_id="env-2", rev=1, host_type="qclaw", instance_id="inst-hd-1",
                    host_version=None, adapter_type="qclaw", connector="conn-2", digest=DIGEST_B)
        _insert_model(conn, mp_id="mp-1", mp_rev=1, env_id="env-1", env_rev=1, strategy="fixed",
                      provider="synthetic-provider", model_id="synthetic-model", digest=DIGEST_A)
        _insert_model(conn, mp_id="mp-2", mp_rev=1, env_id="env-2", env_rev=1,
                      strategy="host-default", provider=None, model_id=None, digest=DIGEST_B)
        _insert_model(conn, mp_id="mp-3", mp_rev=1, env_id="env-1", env_rev=1, strategy="fixed",
                      provider="synthetic-provider-3", model_id="synthetic-model-3",
                      digest=DIGEST_A)

        _seed_cycle(
            conn,
            {"agent_id": MEMBER_FULL, "alias": "Dev-WKB1", "display": "成员A", "env_id": "env-1",
             "mp_id": "mp-1", "enabled": 1},
            {"agent_id": MEMBER_FULL, "binding_rev": 1, "env_id": "env-1", "env_rev": 1,
             "adapter_type": "workbuddy", "connector": "conn-1", "host_agent_id": "host-wkb1",
             "digest": DIGEST_A},
        )
        _seed_cycle(
            conn,
            {"agent_id": MEMBER_DISABLED, "alias": "CoX", "normalized": "cox", "display": "成员B",
             "roles": '["sales-architect"]', "capabilities": '["review","document"]',
             "env_id": "env-1", "mp_id": "mp-1", "enabled": 0},
            {"agent_id": MEMBER_DISABLED, "binding_rev": 1, "env_id": "env-1", "env_rev": 1,
             "adapter_type": "workbuddy", "connector": "conn-1", "host_agent_id": "host-cox",
             "digest": DIGEST_A},
        )
        _seed_cycle(
            conn,
            {"agent_id": MEMBER_HOST_DEFAULT, "display": "成员C", "env_id": "env-2", "mp_id": "mp-2",
             "enabled": 1},
            {"agent_id": MEMBER_HOST_DEFAULT, "binding_rev": 1, "env_id": "env-2", "env_rev": 1,
             "adapter_type": "qclaw", "connector": "conn-2", "host_agent_id": "host-solo",
             "digest": DIGEST_B},
        )
        for agent_id in (MEMBER_NO_CHECKS, MEMBER_CONFIG_ONLY, MEMBER_STALE_BINDING,
                         MEMBER_STALE_MODEL):
            _seed_cycle(
                conn,
                {"agent_id": agent_id, "display": "成员-%s" % agent_id, "env_id": "env-1",
                 "mp_id": "mp-1", "enabled": 1},
                {"agent_id": agent_id, "binding_rev": 1, "env_id": "env-1", "env_rev": 1,
                 "adapter_type": "workbuddy", "connector": "conn-1",
                 "host_agent_id": "host-%s" % agent_id, "digest": DIGEST_A},
            )

        _insert_project_agent(conn, "proj-a", MEMBER_FULL)
        _insert_project_agent(conn, "proj-b", MEMBER_DISABLED, roles='["sales-architect"]')
        _insert_project_agent(conn, "proj-hd", MEMBER_HOST_DEFAULT)
        _insert_project_agent(conn, "proj-none", MEMBER_NO_CHECKS)
        _insert_project_agent(conn, "proj-cfg", MEMBER_CONFIG_ONLY)
        _insert_project_agent(conn, "proj-bd", MEMBER_STALE_BINDING)
        _insert_project_agent(conn, "proj-md", MEMBER_STALE_MODEL)

        _seed_four_checks(conn, MEMBER_FULL, mp_id="mp-1")
        _seed_four_checks(conn, MEMBER_HOST_DEFAULT, mp_id="mp-2", digest=DIGEST_B,
                          env_id="env-2", env_rev=1)
        _insert_check(conn, "chk-%s-config" % MEMBER_CONFIG_ONLY, MEMBER_CONFIG_ONLY, "config")
        _seed_four_checks(conn, MEMBER_STALE_BINDING, mp_id="mp-1", digest=DIGEST_B)
        _seed_four_checks(conn, MEMBER_STALE_MODEL, mp_id="mp-1",
                          model={"mp_id": "mp-3", "mp_rev": 1})

    # -- 便捷入口 ---------------------------------------------------------
    def run_preflight(self, request, caller=None, evaluated_at=EVAL_AT, required_checks=None,
                      registry_id=REGISTRY_ID):
        return preflight_member(
            self.conn, registry_id=registry_id,
            caller=owner_context() if caller is None else caller,
            request=request, evaluated_at=evaluated_at, required_checks=required_checks,
        )

    def full_request(self, project_id="proj-a", **extra):
        request = {"agentId": MEMBER_FULL, "projectId": project_id}
        request.update(extra)
        return request


class AllowTests(PreflightTestCase):
    def test_allow_decision_and_full_shape(self):
        result = self.run_preflight(self.full_request(modelProfileId="mp-1"))
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(list(result), TOP_KEYS)
        self.assertEqual(result["schemaVersion"], "dsh-member-preflight.v1")
        self.assertEqual(
            list(result["registry"]),
            ["registryId", "schemaVersion", "authority", "revision", "sourceDigest"],
        )
        self.assertEqual(result["registry"]["registryId"], REGISTRY_ID)
        self.assertEqual(result["registry"]["authority"], "candidate")
        self.assertEqual(
            list(result["request"]),
            ["agentId", "agentAlias", "normalizedAlias", "projectId", "modelProfileId",
             "evaluatedAt", "requiredChecks"],
        )
        self.assertIsNone(result["request"]["normalizedAlias"])
        self.assertEqual(result["request"]["requiredChecks"], list(REQUIRED_TYPES))
        self.assertEqual(result["caller"]["role"], "owner")
        self.assertEqual(result["caller"]["actorId"], "cox")
        self.assertEqual(result["member"]["agentId"], MEMBER_FULL)
        self.assertEqual(result["project"]["projectId"], "proj-a")
        self.assertEqual([entry["checkType"] for entry in result["checks"]], list(REQUIRED_TYPES))
        for entry in result["checks"]:
            self.assertEqual(list(entry), CHECK_ENTRY_KEYS)
            self.assertTrue(entry["satisfied"])
            self.assertTrue(entry["boundToCurrentReference"])
            self.assertEqual(entry["state"], "satisfied")
        self.assertEqual(
            result["checksSatisfied"],
            {"config": True, "identity": True, "model": True, "runtime": True},
        )

    def test_allow_never_claims_host_or_isolation(self):
        result = self.run_preflight(self.full_request())
        self.assertEqual(list(result["claims"]), [
            "osIsolationVerified", "realHostIdentityVerified", "realModelObserved",
            "declarationsTreatedAsEnforcement", "executableProven",
        ])
        self.assertFalse(result["claims"]["osIsolationVerified"])
        self.assertFalse(result["claims"]["realHostIdentityVerified"])
        self.assertFalse(result["claims"]["declarationsTreatedAsEnforcement"])
        self.assertFalse(result["claims"]["executableProven"])
        # LOCAL 级 model check 不等于实测模型：不提升 realModelObserved。
        self.assertFalse(result["claims"]["realModelObserved"])
        self.assertFalse(result["authority"]["wouldWrite"])
        self.assertFalse(result["authority"]["wouldDispatch"])
        self.assertTrue(result["authority"]["authorityUnchanged"])
        self.assertFalse(result["authority"]["hostStarted"])
        self.assertFalse(result["authority"]["networkAccessed"])
        self.assertFalse(result["authority"]["credentialsAccessed"])
        self.assertTrue(result["authority"]["realDshAuthorizationGateUnchanged"])
        self.assertFalse(result["authority"]["realDshConnected"])

    def test_evidence_dimensions_are_explicit(self):
        result = self.run_preflight(self.full_request())
        self.assertEqual(sorted(result["evidence"]), sorted(EVIDENCE_KEYS))
        for key in EVIDENCE_KEYS:
            self.assertIn(result["evidence"][key]["evidenceLevel"], dmp.EVIDENCE_LEVELS, key)
        self.assertEqual(result["evidence"]["identity"]["evidenceLevel"], "LOCAL")
        self.assertFalse(result["evidence"]["identity"]["declarationOnly"])
        self.assertEqual(result["evidence"]["model"]["evidenceLevel"], "LOCAL")
        self.assertEqual(result["evidence"]["model"]["strategy"], "fixed")
        self.assertEqual(result["evidence"]["model"]["declaredProvider"], "synthetic-provider")
        self.assertEqual(result["evidence"]["model"]["declaredModelId"], "synthetic-model")
        self.assertFalse(result["evidence"]["model"]["actualModelObserved"])
        self.assertFalse(result["evidence"]["model"]["forgedModel"])
        # 声明层一律 UNVERIFIED，且明确不是 OS 隔离 / 真实宿主身份。
        for key in ("environment", "binding", "host", "project", "permissionCeiling"):
            self.assertEqual(result["evidence"][key]["evidenceLevel"], "UNVERIFIED", key)
            self.assertTrue(result["evidence"][key]["declarationOnly"], key)
        self.assertTrue(result["evidence"]["permissionCeiling"]["notOsIsolation"])
        self.assertTrue(result["evidence"]["permissionCeiling"]["notRealHostIdentity"])

    def test_alias_only_resolves_by_normalized_alias(self):
        result = self.run_preflight({"agentAlias": "  DEV-wkb1 ", "projectId": "proj-a"})
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["request"]["normalizedAlias"], "dev-wkb1")
        self.assertIsNone(result["request"]["agentId"])
        self.assertEqual(result["member"]["agentId"], MEMBER_FULL)

    def test_agent_id_and_alias_together_must_agree(self):
        result = self.run_preflight(
            {"agentId": MEMBER_FULL, "agentAlias": "Dev-WKB1", "projectId": "proj-a"}
        )
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["member"]["agentId"], MEMBER_FULL)

    def test_reviewer_context_allowed_and_echoed(self):
        result = self.run_preflight(self.full_request(), caller=reviewer_context())
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["caller"]["role"], "reviewer")

    def test_same_input_same_output(self):
        first = self.run_preflight(self.full_request(modelProfileId="mp-1"))
        second = self.run_preflight(self.full_request(modelProfileId="mp-1"))
        self.assertEqual(first, second)

    def test_result_is_json_serialisable(self):
        result = self.run_preflight(self.full_request())
        self.assertIsInstance(json.dumps(result, ensure_ascii=False, sort_keys=True), str)


class AliasAndIdentityTests(PreflightTestCase):
    def test_unknown_alias_denied_without_fuzzy_match(self):
        result = self.run_preflight({"agentAlias": "dev-wkb-1", "projectId": "proj-a"})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["member_not_found"])
        self.assertEqual(result["reasons"][0]["field"], "request.agentAlias")
        self.assertIsNone(result["member"])
        self.assertIsNone(result["evidence"])

    def test_unknown_agent_id_denied(self):
        result = self.run_preflight({"agentId": "dev-nobody", "projectId": "proj-a"})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["member_not_found"])
        self.assertEqual(result["reasons"][0]["field"], "request.agentId")

    def test_display_name_is_not_an_alias(self):
        result = self.run_preflight({"agentAlias": "成员A", "projectId": "proj-a"})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["member_not_found"])

    def test_agent_id_and_alias_conflict_denied(self):
        result = self.run_preflight(
            {"agentId": MEMBER_FULL, "agentAlias": "CoX", "projectId": "proj-a"}
        )
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["alias_agent_mismatch"])
        self.assertIsNone(result["member"])

    def test_disabled_member_denied(self):
        result = self.run_preflight({"agentId": MEMBER_DISABLED, "projectId": "proj-b"})
        self.assertEqual(result["decision"], "deny")
        self.assertIn("member_disabled", _codes(result))
        self.assertFalse(result["member"]["enabled"])

    def test_registration_incomplete_denied(self):
        real = list_registry_agents(self.conn, registry_id=REGISTRY_ID)
        items = []
        for item in real["items"]:
            entry = dict(item)
            if entry["agentId"] == MEMBER_FULL:
                entry["registrationComplete"] = False
            items.append(entry)
        with mock.patch.object(
            dmp, "list_registry_agents",
            return_value={"items": items, "registry": real["registry"]},
        ):
            result = self.run_preflight(self.full_request())
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["registration_incomplete"])
        self.assertEqual(result["reasons"][0]["field"], "member.registrationComplete")


class ProjectTests(PreflightTestCase):
    def test_project_not_associated_denied(self):
        result = self.run_preflight({"agentId": MEMBER_FULL, "projectId": "proj-z"})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["project_not_associated"])
        self.assertIsNone(result["project"])

    def test_project_of_another_member_denied(self):
        result = self.run_preflight({"agentId": MEMBER_FULL, "projectId": "proj-b"})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["project_not_associated"])

    def test_disabled_project_association_denied(self):
        self.conn.execute(
            "UPDATE project_agents SET enabled = 0 WHERE project_id = 'proj-a' AND agent_id = ?",
            (MEMBER_FULL,),
        )
        result = self.run_preflight(self.full_request())
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["project_disabled"])
        self.assertFalse(result["project"]["enabled"])
        # 项目关联和 permission ceiling 只做声明核验：始终 UNVERIFIED。
        self.assertEqual(result["evidence"]["project"]["evidenceLevel"], "UNVERIFIED")


class ModelTests(PreflightTestCase):
    def test_model_profile_mismatch_denied(self):
        result = self.run_preflight(self.full_request(modelProfileId="mp-2"))
        self.assertEqual(result["decision"], "deny")
        self.assertIn("model_profile_mismatch", _codes(result))
        self.assertEqual(
            [r["field"] for r in result["reasons"] if r["code"] == "model_profile_mismatch"],
            ["request.modelProfileId"],
        )

    def test_host_default_member_never_forges_model(self):
        result = self.run_preflight({"agentId": MEMBER_HOST_DEFAULT, "projectId": "proj-hd"})
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["evidence"]["model"]["strategy"], "host-default")
        self.assertIsNone(result["evidence"]["model"]["declaredProvider"])
        self.assertIsNone(result["evidence"]["model"]["declaredModelId"])
        self.assertFalse(result["evidence"]["model"]["forgedModel"])
        self.assertFalse(result["evidence"]["model"]["actualModelObserved"])
        self.assertIsNone(result["member"]["modelProfile"]["provider"])
        self.assertIsNone(result["member"]["modelProfile"]["modelId"])
        self.assertFalse(result["claims"]["realModelObserved"])

    def test_persisted_current_model_reference_mismatch_fails_closed(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute(
                "UPDATE registry_agents SET model_profile_id = 'mp-x' WHERE agent_id = ?",
                (MEMBER_FULL,),
            )
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")
        code, details = _call(self.conn, registry_id=REGISTRY_ID, caller=owner_context(),
                              request=self.full_request(), evaluated_at=EVAL_AT)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"], "agents.%s.modelProfile" % MEMBER_FULL)
        self.assertFalse(self.conn.in_transaction)


class CheckTests(PreflightTestCase):
    def test_missing_checks_recheck(self):
        result = self.run_preflight({"agentId": MEMBER_NO_CHECKS, "projectId": "proj-none"})
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_missing"] * 4)
        self.assertEqual([entry["state"] for entry in result["checks"]], ["missing"] * 4)
        self.assertIsNone(result["checks"][0]["checkId"])
        self.assertEqual(
            result["checksSatisfied"],
            {"config": False, "identity": False, "model": False, "runtime": False},
        )

    def test_failed_check_denied(self):
        _insert_check(self.conn, "chk-x-identity", MEMBER_NO_CHECKS, "identity", status="failed",
                      failure_reason="宿主身份未确认")
        _insert_check(self.conn, "chk-x-config", MEMBER_NO_CHECKS, "config")
        _insert_check(self.conn, "chk-x-runtime", MEMBER_NO_CHECKS, "runtime")
        _insert_check(self.conn, "chk-x-model", MEMBER_NO_CHECKS, "model", mp_id="mp-1", mp_rev=1)
        result = self.run_preflight({"agentId": MEMBER_NO_CHECKS, "projectId": "proj-none"})
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["check_failed"])
        self.assertEqual(result["checks"][1]["state"], "failed")
        self.assertEqual(result["checks"][1]["checkId"], "chk-x-identity")
        self.assertEqual(result["checks"][1]["status"], "failed")

    def test_expired_check_recheck(self):
        _insert_check(self.conn, "chk-x-config", MEMBER_NO_CHECKS, "config",
                      observed_at=TS_LATER, expires_at=EXPIRED_AT)
        _insert_check(self.conn, "chk-x-identity", MEMBER_NO_CHECKS, "identity")
        _insert_check(self.conn, "chk-x-model", MEMBER_NO_CHECKS, "model", mp_id="mp-1", mp_rev=1)
        _insert_check(self.conn, "chk-x-runtime", MEMBER_NO_CHECKS, "runtime")
        result = self.run_preflight({"agentId": MEMBER_NO_CHECKS, "projectId": "proj-none"})
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_expired"])
        self.assertEqual(result["checks"][0]["state"], "expired")
        self.assertEqual(result["checks"][0]["expiresAt"], EXPIRED_AT)

    def test_expiry_unknown_recheck(self):
        _insert_check(self.conn, "chk-x-config", MEMBER_NO_CHECKS, "config",
                      observed_at=TS_LATER, expires_at=NAIVE_AT)
        _insert_check(self.conn, "chk-x-identity", MEMBER_NO_CHECKS, "identity")
        _insert_check(self.conn, "chk-x-model", MEMBER_NO_CHECKS, "model", mp_id="mp-1", mp_rev=1)
        _insert_check(self.conn, "chk-x-runtime", MEMBER_NO_CHECKS, "runtime")
        result = self.run_preflight({"agentId": MEMBER_NO_CHECKS, "projectId": "proj-none"})
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_expiry_unknown"])
        self.assertEqual(result["checks"][0]["state"], "expiry_unknown")

    def test_unverified_evidence_recheck(self):
        _insert_check(self.conn, "chk-x-config", MEMBER_NO_CHECKS, "config")
        _insert_check(self.conn, "chk-x-identity", MEMBER_NO_CHECKS, "identity")
        _insert_check(self.conn, "chk-x-model", MEMBER_NO_CHECKS, "model", mp_id="mp-1", mp_rev=1)
        _insert_check(self.conn, "chk-x-runtime", MEMBER_NO_CHECKS, "runtime",
                      status="passed", evidence_level="UNVERIFIED")
        result = self.run_preflight({"agentId": MEMBER_NO_CHECKS, "projectId": "proj-none"})
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_evidence_unverified"])
        self.assertEqual(result["evidence"]["runtime"]["evidenceLevel"], "UNVERIFIED")
        self.assertTrue(result["evidence"]["runtime"]["declarationOnly"])

    def test_unverified_status_recheck(self):
        _insert_check(self.conn, "chk-x-config", MEMBER_NO_CHECKS, "config")
        _insert_check(self.conn, "chk-x-identity", MEMBER_NO_CHECKS, "identity",
                      status="unverified", evidence_level="OFFICIAL")
        _insert_check(self.conn, "chk-x-model", MEMBER_NO_CHECKS, "model", mp_id="mp-1", mp_rev=1)
        _insert_check(self.conn, "chk-x-runtime", MEMBER_NO_CHECKS, "runtime")
        result = self.run_preflight({"agentId": MEMBER_NO_CHECKS, "projectId": "proj-none"})
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_unverified"])
        # 未完成核验不得被当成有效证据。
        self.assertEqual(result["evidence"]["identity"]["evidenceLevel"], "UNVERIFIED")

    def test_stale_check_by_old_binding_revision(self):
        _insert_binding(self.conn, MEMBER_FULL, binding_rev=2, env_id="env-1", env_rev=1,
                        adapter_type="workbuddy", connector="conn-1", host_agent_id="host-wkb1",
                        digest=DIGEST_A)
        self.conn.execute(
            "UPDATE registry_agents SET current_binding_revision = 2 WHERE agent_id = ?",
            (MEMBER_FULL,),
        )
        result = self.run_preflight(self.full_request())
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_stale"] * 4)
        for entry in result["checks"]:
            self.assertFalse(entry["boundToCurrentReference"])
            self.assertEqual(entry["state"], "stale")

    def test_stale_check_by_config_digest(self):
        result = self.run_preflight({"agentId": MEMBER_STALE_BINDING, "projectId": "proj-bd"})
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_stale"] * 4)

    def test_stale_model_check_by_old_model_profile(self):
        result = self.run_preflight({"agentId": MEMBER_STALE_MODEL, "projectId": "proj-md"})
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_stale"])
        self.assertEqual(result["checks"][2]["checkType"], "model")
        self.assertFalse(result["checks"][2]["boundToCurrentReference"])

    def test_foreign_check_binding_fails_closed(self):
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            _insert_check(self.conn, "chk-foreign-binding", MEMBER_CONFIG_ONLY, "config",
                          binding_rev=7)
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")
        code, details = _call(self.conn, registry_id=REGISTRY_ID, caller=owner_context(),
                              request={"agentId": MEMBER_CONFIG_ONLY, "projectId": "proj-cfg"},
                              evaluated_at=EVAL_AT)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "missing_reference")
        self.assertEqual(details["field"],
                         "agents.%s.checks.chk-foreign-binding.bindingRevision" % MEMBER_CONFIG_ONLY)

    def test_required_checks_subset(self):
        """R2 修正：调用方不得缩减必查集合；不传参时仍是完整四类必查。"""
        # R1 旧断言是 ("config",) 能 allow（独立 Review 判定为缺陷），现改为受控拒绝。
        code, details = _call(self.conn, registry_id=REGISTRY_ID, caller=owner_context(),
                              request={"agentId": MEMBER_CONFIG_ONLY, "projectId": "proj-cfg"},
                              evaluated_at=EVAL_AT, required_checks=("config",))
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["field"], "requiredChecks")
        self.assertEqual(details["reason"], "required_checks_not_full")

        strict = self.run_preflight({"agentId": MEMBER_CONFIG_ONLY, "projectId": "proj-cfg"})
        self.assertEqual(strict["decision"], "recheck")
        self.assertEqual(_codes(strict), ["check_missing"] * 3)
        self.assertEqual(
            [entry["checkType"] for entry in strict["checks"]], list(REQUIRED_TYPES)
        )


class CallerGateTests(PreflightTestCase):
    def test_worker_caller_denied(self):
        code, details = _call(self.conn, registry_id=REGISTRY_ID, caller=worker_context(),
                              request=self.full_request(), evaluated_at=EVAL_AT)
        self.assertEqual(code, "permission_denied")
        self.assertEqual(details["role"], "worker")

    def test_source_caller_denied(self):
        code, details = _call(self.conn, registry_id=REGISTRY_ID, caller=source_context(),
                              request=self.full_request(), evaluated_at=EVAL_AT)
        self.assertEqual(code, "permission_denied")
        self.assertEqual(details["role"], "source")

    def test_missing_caller_denied(self):
        code, _details = _call(self.conn, registry_id=REGISTRY_ID, caller=None,
                               request=self.full_request(), evaluated_at=EVAL_AT)
        self.assertEqual(code, "permission_denied")

    def test_foreign_caller_type_denied(self):
        code, details = _call(self.conn, registry_id=REGISTRY_ID, caller={"role": "owner"},
                              request=self.full_request(), evaluated_at=EVAL_AT)
        self.assertEqual(code, "permission_denied")
        self.assertEqual(details["type"], "dict")

    def test_self_reported_identity_in_request_denied(self):
        for field in ("role", "caller", "permissionCeiling", "enabled", "normalizedAlias",
                      "decision", "evidenceLevel", "writeScope"):
            with self.subTest(field=field):
                request = self.full_request()
                request[field] = "self-reported"
                code, details = _call(self.conn, registry_id=REGISTRY_ID, caller=owner_context(),
                                      request=request, evaluated_at=EVAL_AT)
                self.assertEqual(code, "invalid_task")
                self.assertEqual(details["reason"], "self_reported_identity_not_allowed")
                self.assertEqual(details["field"], "request.%s" % field)

    def test_unknown_request_field_denied(self):
        request = self.full_request()
        request["projectRole"] = "skill-worker"
        code, details = _call(self.conn, registry_id=REGISTRY_ID, caller=owner_context(),
                              request=request, evaluated_at=EVAL_AT)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "unknown_request_field")
        self.assertEqual(details["field"], "request.projectRole")


class ArgumentValidationTests(PreflightTestCase):
    def _assert_invalid(self, field, reason=None, **kwargs):
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            code, details = _call(self.conn, **kwargs)
        finally:
            self.conn.set_trace_callback(None)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["field"], field)
        if reason is not None:
            self.assertEqual(details["reason"], reason)
        self.assertEqual(seen, [], "非法入参不得执行任何查询")
        self.assertFalse(self.conn.in_transaction)

    def _kwargs(self, request=None, evaluated_at=EVAL_AT, required_checks=None):
        return {
            "registry_id": REGISTRY_ID,
            "caller": owner_context(),
            "request": self.full_request() if request is None else request,
            "evaluated_at": evaluated_at,
            "required_checks": required_checks,
        }

    def test_registry_id_variants(self):
        for bad in (None, "", "reg-1\n", "reg 1", "reg-1 ", 1, b"reg-1", "../reg-1"):
            with self.subTest(registry_id=bad):
                kwargs = self._kwargs()
                kwargs["registry_id"] = bad
                self._assert_invalid("registryId", "invalid_argument", **kwargs)

    def test_request_must_be_object(self):
        for bad in (None, "agentId=dev-wkb1", 1, ["dev-wkb1"]):
            with self.subTest(request=bad):
                kwargs = self._kwargs()
                kwargs["request"] = bad
                self._assert_invalid("request", "invalid_argument", **kwargs)

    def test_request_requires_identity_and_project(self):
        self._assert_invalid("request", "invalid_argument",
                             **self._kwargs(request={"projectId": "proj-a"}))
        self._assert_invalid("request.projectId", "invalid_argument",
                             **self._kwargs(request={"agentId": MEMBER_FULL}))
        self._assert_invalid("request.projectId", "invalid_argument",
                             **self._kwargs(request={"agentId": MEMBER_FULL, "projectId": None}))
        self._assert_invalid("request.agentId", "invalid_argument",
                             **self._kwargs(request={"agentId": None, "agentAlias": "Dev-WKB1",
                                                     "projectId": "proj-a"}))

    def test_request_id_variants(self):
        for bad in ("", " dev-wkb1", "dev-wkb1\n", "dev/wkb1", "https://x", "dev wkb1", 1, True):
            with self.subTest(agent_id=bad):
                self._assert_invalid(
                    "request.agentId", "invalid_argument",
                    **self._kwargs(request={"agentId": bad, "projectId": "proj-a"}),
                )

    def test_request_alias_variants(self):
        for bad in ("", "   ", 1, True, ["dev-wkb1"], "dev\u0000wkb1", "x" * 200):
            with self.subTest(agent_alias=bad):
                self._assert_invalid(
                    "request.agentAlias", "invalid_argument",
                    **self._kwargs(request={"agentAlias": bad, "projectId": "proj-a"}),
                )

    def test_evaluated_at_variants(self):
        for bad in (None, "", "not-a-time", "2026-10-08T00:00:00", "2026-10-08", 1, True):
            with self.subTest(evaluated_at=bad):
                self._assert_invalid("evaluatedAt", "invalid_argument",
                                     **self._kwargs(evaluated_at=bad))
        # 带 Z 的 UTC 形式等价（同一时刻）必须被接受。
        result = self.run_preflight(self.full_request(), evaluated_at="2026-10-07T19:00:00Z")
        self.assertEqual(result["decision"], "allow")

    def test_required_checks_variants(self):
        for bad in ((), [], ["config", "config"], ["CONFIG"], ["unknown-check"], "config",
                    ["config", "login", "config"]):
            with self.subTest(required_checks=bad):
                self._assert_invalid("requiredChecks", "invalid_argument",
                                     **self._kwargs(required_checks=bad))
        # R2 修正：合法但不等于完整四类的集合（此处含额外 login）同样拒绝，
        # 不再像 R1 那样返回 recheck；完整四类集合本身仍被接受（见 RequiredChecksContractTests）。
        code, details = _call(self.conn, **self._kwargs(required_checks=["config", "login"]))
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["field"], "requiredChecks")
        self.assertEqual(details["reason"], "required_checks_not_full")

    def test_connection_and_row_factory(self):
        with self.assertRaises(ContractError) as ctx:
            preflight_member(None, registry_id=REGISTRY_ID, caller=owner_context(),
                             request=self.full_request(), evaluated_at=EVAL_AT)
        self.assertEqual(ctx.exception.details["reason"], "connection_required")
        self.conn.row_factory = lambda _cursor, row: row
        try:
            code, details = _call(self.conn, **self._kwargs())
        finally:
            self.conn.row_factory = sqlite3.Row
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "unsupported_row_factory")

    def test_caller_transaction_preserved(self):
        self.conn.execute("BEGIN IMMEDIATE")
        self.conn.execute(
            "INSERT INTO sources (source_id, label, registered_at) VALUES ('src-tx','tx',?)", (TS,)
        )
        try:
            code, details = _call(self.conn, **self._kwargs())
        finally:
            self.assertTrue(self.conn.in_transaction, "caller 事务必须原样保留")
            self.conn.execute("ROLLBACK")
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "connection_in_transaction")
        self.assertFalse(
            self.conn.execute("SELECT 1 FROM sources WHERE source_id = 'src-tx'").fetchone(),
            "caller 未提交事务不得被服务提交",
        )


class SchemaStateTests(PreflightTestCase):
    def test_unmigrated_registry_rejected(self):
        store = SqliteStore(":memory:")
        try:
            store.conn.execute(
                "INSERT INTO sources (source_id, label, registered_at) VALUES ('src-1','s',?)", (TS,)
            )
            code, details = _call(store.conn, registry_id=REGISTRY_ID, caller=owner_context(),
                                  request=self.full_request(), evaluated_at=EVAL_AT)
            self.assertFalse(store.conn.in_transaction)
        finally:
            store.close()
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "registry_unmigrated")

    def test_wrong_registry_id_rejected(self):
        code, details = _call(self.conn, registry_id="reg-2", caller=owner_context(),
                              request=self.full_request(), evaluated_at=EVAL_AT)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["field"], "registryId")
        self.assertEqual(details["reason"], "registry_id_mismatch")


class ReadOnlyTests(PreflightTestCase):
    def test_trace_contains_only_reads_and_own_transaction(self):
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            self.run_preflight(self.full_request())
        finally:
            self.conn.set_trace_callback(None)
        self.assertTrue(seen, "trace 必须捕获到语句")
        self.assertTrue(any(s.strip().upper().startswith("SELECT") for s in seen))
        self.assertTrue(any(s.strip().upper() == "BEGIN" for s in seen))
        self.assertTrue(any(s.strip().upper() == "COMMIT" for s in seen))
        for statement in seen:
            head = statement.strip().split(" ")[0].upper()
            self.assertIn(head, ("SELECT", "BEGIN", "COMMIT", "ROLLBACK"), statement)
            # 按词边界判写语句 / pragma，避免把 created_at 这类列名误判为 CREATE。
            upper = statement.upper() + " "
            for forbidden in ("PRAGMA ", "INSERT ", "UPDATE ", "DELETE ", "CREATE ", "DROP ",
                              "REPLACE ", "ATTACH ", "VACUUM ", "ALTER "):
                self.assertNotIn(forbidden, upper, statement)

    def test_no_write_and_no_schema_change(self):
        before_dump = _dump_tables(self.conn)
        before_changes = self.conn.total_changes
        before_factory = self.conn.row_factory
        before_isolation = self.conn.isolation_level
        result = self.run_preflight(self.full_request())
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(_dump_tables(self.conn), before_dump)
        self.assertEqual(self.conn.total_changes, before_changes)
        self.assertIs(self.conn.row_factory, before_factory)
        self.assertEqual(self.conn.isolation_level, before_isolation)
        self.assertFalse(self.conn.in_transaction)

    def test_read_only_connection_succeeds(self):
        self.conn.execute("PRAGMA query_only = ON")
        try:
            first = self.run_preflight(self.full_request())
            second = self.run_preflight(self.full_request())
            with self.assertRaises(sqlite3.OperationalError):
                self.conn.execute("UPDATE registry_agents SET revision = 2 WHERE agent_id = ?",
                                  (MEMBER_FULL,))
            self.assertFalse(self.conn.in_transaction)
        finally:
            self.conn.execute("PRAGMA query_only = OFF")
        self.assertEqual(first["decision"], "allow")
        self.assertEqual(first, second)

    def test_service_works_with_host_access_patched_out(self):
        with mock.patch("builtins.open", side_effect=RuntimeError("no open")), \
                mock.patch.object(sqlite3, "connect", side_effect=RuntimeError("no connect")), \
                mock.patch("socket.socket", side_effect=RuntimeError("no socket")), \
                mock.patch.object(subprocess, "Popen", side_effect=RuntimeError("no popen")):
            result = self.run_preflight(self.full_request())
        self.assertEqual(result["decision"], "allow")
        self.assertFalse(result["authority"]["hostStarted"])
        self.assertFalse(result["authority"]["networkAccessed"])
        self.assertFalse(result["authority"]["credentialsAccessed"])

    def test_failure_rolls_back_only_own_transaction(self):
        before = _dump_tables(self.conn)
        before_changes = self.conn.total_changes
        self.conn.execute(
            "UPDATE registry_agents SET roles_json = '[]' WHERE agent_id = ?", (MEMBER_FULL,)
        )
        with self.assertRaises(ContractError):
            self.run_preflight(self.full_request())
        self.assertFalse(self.conn.in_transaction)
        self.conn.execute(
            "UPDATE registry_agents SET roles_json = '[\"skill-worker\"]' WHERE agent_id = ?",
            (MEMBER_FULL,),
        )
        self.assertEqual(_dump_tables(self.conn), before)
        self.assertEqual(self.conn.total_changes, before_changes + 2)

    def test_closed_connection_not_wrapped(self):
        self.conn.close()
        with self.assertRaises(sqlite3.DatabaseError) as ctx:
            self.run_preflight(self.full_request())
        self.assertNotIsInstance(ctx.exception, ContractError)


class LatestCheckSelectionTests(PreflightTestCase):
    """R2：每种 check 只按绑定当前引用中“最新 observedAt”的那一条记录判定。

    全部用例使用 MEMBER_NO_CHECKS（env-1 / mp-1 / binding rev 1 / DIGEST_A，默认无 check）；
    ``agent_checks`` 不可变，因此只 INSERT 追加记录，不做 UPDATE / DELETE。
    """

    # 跨时区 / 同刻用例的观察时刻（真实时刻都早于 EVAL_AT = 2026-10-08T03:00:00+08:00）。
    OBS_1800Z = "2026-10-07T18:00:00+00:00"            # = 2026-10-08T02:00:00+08:00
    OBS_1800Z_PLUS8 = "2026-10-08T02:00:00+08:00"      # 与 OBS_1800Z 同一时刻
    OBS_1830Z = "2026-10-07T18:30:00+00:00"            # = 2026-10-08T02:30:00+08:00
    OBS_1830Z_PLUS8 = "2026-10-08T02:30:00+08:00"      # 与 OBS_1830Z 同一时刻

    def _seed_valid_member(self):
        """给 MEMBER_NO_CHECKS 补齐四类绑定当前引用的有效 check（默认 observed_at=TS）。"""
        _seed_four_checks(self.conn, MEMBER_NO_CHECKS, mp_id="mp-1")

    def _preflight(self):
        return self.run_preflight({"agentId": MEMBER_NO_CHECKS, "projectId": "proj-none"})

    def test_newer_passed_overrides_older_failed(self):
        self._seed_valid_member()
        _insert_check(self.conn, "chk-a-config-failed", MEMBER_NO_CHECKS, "config",
                      status="failed", observed_at=TS, expires_at=FUTURE,
                      failure_reason="旧的合成失败记录")
        _insert_check(self.conn, "chk-b-config-passed", MEMBER_NO_CHECKS, "config",
                      observed_at=TS_LATER, expires_at=FUTURE)
        result = self._preflight()
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["reasons"], [])
        entry = result["checks"][0]
        self.assertEqual(entry["checkType"], "config")
        self.assertEqual(entry["state"], "satisfied")
        self.assertEqual(entry["checkId"], "chk-b-config-passed")
        self.assertTrue(entry["satisfied"])

    def test_newer_failed_overrides_older_passed(self):
        self._seed_valid_member()
        _insert_check(self.conn, "chk-z-config-failed", MEMBER_NO_CHECKS, "config",
                      status="failed", observed_at=TS_LATER, expires_at=FUTURE,
                      failure_reason="较新的合成失败记录")
        result = self._preflight()
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["check_failed"])
        entry = result["checks"][0]
        self.assertEqual(entry["state"], "failed")
        self.assertEqual(entry["checkId"], "chk-z-config-failed")
        self.assertFalse(entry["satisfied"])

    def test_newer_expired_check_never_falls_back_to_older_valid(self):
        self._seed_valid_member()
        _insert_check(self.conn, "chk-z-config-expired", MEMBER_NO_CHECKS, "config",
                      observed_at=TS_LATER, expires_at=EXPIRED_AT)
        result = self._preflight()
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_expired"])
        entry = result["checks"][0]
        self.assertEqual(entry["state"], "expired")
        self.assertEqual(entry["checkId"], "chk-z-config-expired")
        self.assertEqual(entry["expiresAt"], EXPIRED_AT)
        self.assertFalse(entry["satisfied"])

    def test_newer_expiry_unknown_never_falls_back_to_older_valid(self):
        self._seed_valid_member()
        _insert_check(self.conn, "chk-z-config-unknown", MEMBER_NO_CHECKS, "config",
                      observed_at=TS_LATER, expires_at=NAIVE_AT)
        result = self._preflight()
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_expiry_unknown"])
        entry = result["checks"][0]
        self.assertEqual(entry["state"], "expiry_unknown")
        self.assertEqual(entry["checkId"], "chk-z-config-unknown")
        self.assertFalse(entry["satisfied"])

    def test_failure_recovery_after_older_expired_record(self):
        self._seed_valid_member()
        _insert_check(self.conn, "chk-a-config-expired", MEMBER_NO_CHECKS, "config",
                      observed_at=TS, expires_at=EXPIRED_AT)
        _insert_check(self.conn, "chk-z-config-fresh", MEMBER_NO_CHECKS, "config",
                      observed_at=TS_LATER, expires_at=FUTURE)
        result = self._preflight()
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["reasons"], [])
        entry = result["checks"][0]
        self.assertEqual(entry["state"], "satisfied")
        self.assertEqual(entry["checkId"], "chk-z-config-fresh")

    def test_observed_at_compares_timezone_aware_not_lexically(self):
        """字符串序里 +08:00 写法更大，但真实时刻更早：必须按真实时刻取更晚的失败记录。"""
        self.assertEqual(
            datetime.fromisoformat(self.OBS_1800Z_PLUS8), datetime.fromisoformat(self.OBS_1800Z)
        )
        self.assertLess(self.OBS_1800Z, self.OBS_1830Z)
        self._seed_valid_member()
        _insert_check(self.conn, "chk-a-config-passed", MEMBER_NO_CHECKS, "config",
                      observed_at=self.OBS_1800Z_PLUS8, expires_at=FUTURE)
        _insert_check(self.conn, "chk-b-config-failed", MEMBER_NO_CHECKS, "config",
                      status="failed", observed_at=self.OBS_1830Z, expires_at=FUTURE,
                      failure_reason="真实时刻更晚的合成失败记录")
        result = self._preflight()
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["check_failed"])
        self.assertEqual(result["checks"][0]["checkId"], "chk-b-config-failed")

    def test_observed_at_compares_timezone_aware_reverse_direction(self):
        """反方向：真实时刻更晚的 passed 记录必须赢过按字符串序更大的 failed 记录。"""
        self._seed_valid_member()
        _insert_check(self.conn, "chk-a-config-failed", MEMBER_NO_CHECKS, "config",
                      status="failed", observed_at=self.OBS_1800Z_PLUS8, expires_at=FUTURE,
                      failure_reason="真实时刻更早的合成失败记录")
        _insert_check(self.conn, "chk-b-config-passed", MEMBER_NO_CHECKS, "config",
                      observed_at=self.OBS_1830Z, expires_at=FUTURE)
        result = self._preflight()
        self.assertEqual(result["decision"], "allow")
        self.assertEqual(result["reasons"], [])
        self.assertEqual(result["checks"][0]["checkId"], "chk-b-config-passed")

    def test_same_instant_tie_uses_documented_checkid_order(self):
        """等时刻（含跨时区同刻）按文档化的 checkId 升序取第一条，且结果可重复。"""
        self.assertEqual(
            datetime.fromisoformat(self.OBS_1830Z_PLUS8), datetime.fromisoformat(self.OBS_1830Z)
        )
        self._seed_valid_member()
        _insert_check(self.conn, "chk-a-config-tie", MEMBER_NO_CHECKS, "config",
                      status="failed", observed_at=self.OBS_1830Z, expires_at=FUTURE,
                      failure_reason="同刻合成失败记录")
        _insert_check(self.conn, "chk-b-config-tie", MEMBER_NO_CHECKS, "config",
                      observed_at=self.OBS_1830Z_PLUS8, expires_at=FUTURE)
        first = self._preflight()
        second = self._preflight()
        self.assertEqual(first, second)
        self.assertEqual(first["decision"], "deny")
        self.assertEqual(_codes(first), ["check_failed"])
        entry = first["checks"][0]
        self.assertEqual(entry["checkId"], "chk-a-config-tie")
        self.assertEqual(entry["state"], "failed")

    def test_invalid_observed_at_fails_closed_not_allow(self):
        """当前绑定记录 observedAt 不可解析：recheck，绝不用旧的仍有效记录放行。"""
        self._seed_valid_member()
        _insert_check(self.conn, "chk-z-config-bad-time", MEMBER_NO_CHECKS, "config",
                      observed_at="not-a-timestamp", expires_at=FUTURE)
        result = self._preflight()
        self.assertNotEqual(result["decision"], "allow")
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_observed_at_invalid"])
        entry = result["checks"][0]
        self.assertEqual(entry["state"], "observed_at_invalid")
        self.assertEqual(entry["checkId"], "chk-z-config-bad-time")
        self.assertFalse(entry["satisfied"])
        self.assertTrue(entry["boundToCurrentReference"])

    def test_naive_observed_at_fails_closed_not_allow(self):
        """无时区的朴素 observedAt 视为无效：同样 recheck，不采用旧记录。"""
        self._seed_valid_member()
        _insert_check(self.conn, "chk-z-config-naive-time", MEMBER_NO_CHECKS, "config",
                      observed_at=NAIVE_AT, expires_at=FUTURE)
        result = self._preflight()
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_observed_at_invalid"])
        self.assertEqual(result["checks"][0]["state"], "observed_at_invalid")

    def test_invalid_observed_at_with_failed_status_still_recheck(self):
        """无效 observedAt 的 failed 记录也保守归为 recheck（文档化：不放行即可）。"""
        self._seed_valid_member()
        _insert_check(self.conn, "chk-z-config-bad-time-failed", MEMBER_NO_CHECKS, "config",
                      status="failed", observed_at="2026-10-08T00:00:00", expires_at=FUTURE,
                      failure_reason="时区无效的合成失败记录")
        result = self._preflight()
        self.assertNotEqual(result["decision"], "allow")
        self.assertEqual(result["decision"], "recheck")
        self.assertEqual(_codes(result), ["check_observed_at_invalid"])

    def test_stale_only_when_no_current_bound_record(self):
        """存在绑定当前引用的记录时，按当前绑定记录判定；旧绑定记录不得抢占成 stale。"""
        # MEMBER_FULL 自带四条绑定当前引用的有效 check（DIGEST_A / binding rev 1）。
        _insert_check(self.conn, "chk-stale-config", MEMBER_FULL, "config",
                      observed_at=TS_LATER, expires_at=FUTURE, digest=DIGEST_B)
        _insert_check(self.conn, "chk-new-config-failed", MEMBER_FULL, "config",
                      status="failed", observed_at=TS_LATER, expires_at=FUTURE,
                      digest=DIGEST_A, failure_reason="绑定当前引用的合成失败记录")
        result = self.run_preflight(self.full_request())
        self.assertEqual(result["decision"], "deny")
        self.assertEqual(_codes(result), ["check_failed"])
        entry = result["checks"][0]
        self.assertEqual(entry["state"], "failed")
        self.assertEqual(entry["checkId"], "chk-new-config-failed")
        self.assertTrue(entry["boundToCurrentReference"])


class RequiredChecksContractTests(PreflightTestCase):
    """R2：完整派发预检固定要求 config / identity / model / runtime，调用方不得缩小必查集合。"""

    def _call_with_trace(self, request, required_checks):
        """调用并返回 ``(code, details, trace)``；trace 用于证明拒绝发生在任何查询之前。"""
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            code, details = _call(self.conn, registry_id=REGISTRY_ID, caller=owner_context(),
                                  request=request, evaluated_at=EVAL_AT,
                                  required_checks=required_checks)
        finally:
            self.conn.set_trace_callback(None)
        return code, details, seen

    def test_narrowed_required_checks_never_allow(self):
        request = {"agentId": MEMBER_CONFIG_ONLY, "projectId": "proj-cfg"}
        for bad in (("config",), ("config", "identity"), ("config", "identity", "model"),
                    ["runtime", "model", "identity"], ("login",)):
            with self.subTest(required_checks=bad):
                code, details, seen = self._call_with_trace(request, bad)
                self.assertEqual(code, "invalid_task")
                self.assertEqual(details["field"], "requiredChecks")
                self.assertEqual(details["reason"], "required_checks_not_full")
                self.assertEqual(seen, [], "非法必查集合不得执行任何查询")
                self.assertFalse(self.conn.in_transaction)

    def test_extended_required_checks_rejected(self):
        request = {"agentId": MEMBER_FULL, "projectId": "proj-a"}
        for bad in (("config", "identity", "model", "runtime", "login"),
                    ("config", "login", "identity", "model", "runtime")):
            with self.subTest(required_checks=bad):
                code, details, seen = self._call_with_trace(request, bad)
                self.assertEqual(code, "invalid_task")
                self.assertEqual(details["reason"], "required_checks_not_full")
                self.assertEqual(seen, [])

    def test_duplicate_or_illegal_required_checks_rejected(self):
        request = {"agentId": MEMBER_FULL, "projectId": "proj-a"}
        for bad in (("config", "identity", "model", "runtime", "runtime"),
                    ("config", "identity", "model", "runtime", 1),
                    ("config", "identity", "model", "runtime", "RUNTIME"),
                    "config",
                    {"config", "identity", "model", "runtime"},
                    ()):
            with self.subTest(required_checks=bad):
                code, details, seen = self._call_with_trace(request, bad)
                self.assertEqual(code, "invalid_task")
                self.assertEqual(details["field"], "requiredChecks")
                self.assertEqual(details["reason"], "invalid_argument")
                self.assertEqual(seen, [])

    def test_full_required_checks_accepted_in_any_order(self):
        for order in (REQUIRED_TYPES, tuple(reversed(REQUIRED_TYPES)),
                      ("runtime", "config", "model", "identity")):
            with self.subTest(required_checks=order):
                result = self.run_preflight(self.full_request(), required_checks=order)
                self.assertEqual(result["decision"], "allow")
                # 输出恒为规范顺序，与传入顺序无关。
                self.assertEqual(result["request"]["requiredChecks"], list(REQUIRED_TYPES))
                self.assertEqual([entry["checkType"] for entry in result["checks"]],
                                 list(REQUIRED_TYPES))
                self.assertEqual(
                    result["checksSatisfied"],
                    {"config": True, "identity": True, "model": True, "runtime": True},
                )

    def test_default_required_checks_match_full_set(self):
        result = self.run_preflight(self.full_request())
        self.assertEqual(result["request"]["requiredChecks"], list(dmp.DEFAULT_REQUIRED_CHECKS))
        self.assertEqual(tuple(dmp.DEFAULT_REQUIRED_CHECKS), REQUIRED_TYPES)


class ArtifactSha256Tests(unittest.TestCase):
    def test_print_artifact_sha256_for_receipt(self):
        """打印两个交付文件的 SHA-256，供交付报告引用（与时间无关，可重复）。"""
        product_path = Path(dmp.__file__).resolve()
        test_path = Path(__file__).resolve()
        product_sha = "sha256:" + hashlib.sha256(product_path.read_bytes()).hexdigest()
        test_sha = "sha256:" + hashlib.sha256(test_path.read_bytes()).hexdigest()
        print("[D4-DSH-MEMBER-MODEL-PREFLIGHT] product_sha256=%s test_file_sha256=%s"
              % (product_sha, test_sha))
        self.assertTrue(product_sha.startswith("sha256:"))
        self.assertTrue(test_sha.startswith("sha256:"))


if __name__ == "__main__":
    unittest.main()
