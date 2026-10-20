"""D4-DSH-DEVELOPMENT-ADAPTER-OFFLINE-001 定向测试：开发 Adapter 离线接线工厂。

覆盖范围（对应任务卡 acceptance）：

- [1] 只新增两个指定产品文件；本文件在被授权入口内用 ``py_compile`` 做语法门禁，并打印
  两个交付文件的实测 SHA-256 供回执引用。
- [2] 公开工厂的注入面：受控 ``CallerContext``、只读 registry 连接、显式 task/attempt、
  来源路径与 SHA、受控 node/sdk/runtime/state/store 配置；``deny`` / ``recheck`` / 身份非法
  一律**零 transport 构造**；工厂自己调用 ``preflight_member``，不接受 payload 自报身份，
  也不接受调用方自带的“已通过预检”对象。
- [3] 成员 / 环境 / 模型 / binding 一致性：``hostType=dsh``、``adapterType=dsh-sdk``、
  ``executionProfileRef=sdk-minimal``、``hostAgentId=D4_DSH_SDK_AGENT_ID``、``fixed`` +
  provider/model 非空、task ``projectId`` / ``agent`` 与预检一致。
- [4] 目录与权限上限：成员上限复用 ``list_registry_agents``，环境上限按已核
  ``environmentId`` + ``revision`` 参数化只读 ``SELECT``；成员 / 环境 ``allowedRoots`` 覆盖
  workspace 与写范围，成员 / 环境 / 项目 ceiling 覆盖任务实际权限；拒相对根、``..``、
  用户符号链接段；四个高风险开关显式 ``false``。
- [5] ``build_development_context`` 先于 ``prepare_dsh_launch``；三份输入 SHA 由工厂可信参数
  注入；错误不回显文件正文。
- [6] ``prepare_dsh_launch`` 的 ``command`` / ``cwd`` / ``env`` 用于 transport 构造；
  只在已验证 env 副本上追加唯一固定键 ``DSH_SYSTEM_PROMPT``；不继承父进程环境 / 凭据。
- [7] ``DshDevelopmentAdapter`` 复用 ``DshSdkAdapter``，只追加 canonical JSON 上下文并参与
  幂等摘要；构造时冻结上下文快照；上下文不同的同 attempt 重投触发 ``DSH_ATTEMPT_CONFLICT``
  且旧账本不被覆盖；transport 构造后 env 不再修改。
- [8] 工厂只返回 Adapter，不 start / submit，不建 D4 任务 / attempt / review；
  生产授权门不被触碰；默认构造 legacy transport 且不运行。
- [9] 全部离线：内存 SQLite（``SqliteStore(":memory:")`` + 既有候选迁移器）+ 假 transport；
  测试内 patch 掉 ``orchestrator.isolation.tempfile.mkdtemp``，私有 HOME 落本 attempt
  receipts 目录下的 fixture 内，零真实 DSH / 模型 / 网络 / 凭据 / 真实子进程。
- [10] 构造前复核成员 / 环境 / 模型 / binding / check 引用；并发漂移即拒绝且零 transport，
  不刷新来源、不改旧 attempt。
- [11] 开工先在测试内核验三份依赖 SHA（``dsh_launch.py`` / ``development_context.py`` /
  ``dsh_member_preflight.py``），漂移即失败停止。

fixture 全部落本 attempt receipts 目录 ``receipts/task-demo-c600ee34ad/att-demo-ba85d7843b/``
之下新建的独立目录，结束即删除；不触碰任何历史 attempt 目录。
"""
import hashlib
import json
import os
from pathlib import Path
import py_compile
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from unittest import mock

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, WORKSPACE_ROOT)

import adapters.dsh_development as dsh_development  # noqa: E402
from adapters.dsh_development import (  # noqa: E402
    DshDevelopmentAdapter,
    build_development_adapter,
)
from adapters.dsh_sdk import (  # noqa: E402
    D4_DSH_SDK_AGENT_ID,
    DSH_SDK_ATTEMPT_CONFLICT,
    DSH_SDK_PROFILE,
    DSH_SDK_SOURCE_ID,
    DshSdkAdapter,
)
from contracts import development_context as development_context_module  # noqa: E402
from contracts.schemas import ContractError  # noqa: E402
from orchestrator import isolation  # noqa: E402
from orchestrator.dsh import DSHAdapter as LegacyDshTransport  # noqa: E402
from orchestrator.isolation import IsolationError  # noqa: E402
from service.context import owner_context, source_context, worker_context  # noqa: E402
from storage.registry_migrations import apply_registry_candidate_migration  # noqa: E402
from storage.sqlite_store import SqliteStore  # noqa: E402

ROLE_PATH = development_context_module.ROLE_SOURCE_ABSOLUTE
#: 成员 / 环境 allowedRoots 与 ceiling 覆盖的受控根（fixture workspace 在其下）。
ROOT = "/Users/demo/.hermes/skills/conductor"
#: 明确不覆盖 fixture workspace 的绝对根（用于“覆盖不足”负例）。
NOT_COVERING_ROOT = "/private/tmp"

RECEIPT_ROOT = os.path.join(WORKSPACE_ROOT, "receipts", "task-demo-c600ee34ad",
                            "att-demo-ba85d7843b")
FIXTURE_PREFIX = "fixture-dsh-dev-"
COMPILE_PREFIX = "py-compile-"

REGISTRY_ID = "reg-dsh-dev"
SOURCE_DIGEST = "sha256:" + "0" * 64
DIGEST_A = "sha256:" + "a" * 64
MEMBER_ID = D4_DSH_SDK_AGENT_ID
PROJECT_ID = "d4-product"
ENV_ID = "env-dsh-1"
MODEL_ID = "mp-dsh-fixed"
TS = "2026-10-10T00:00:00+08:00"
EVAL_AT = "2026-10-10T03:00:00+08:00"
FUTURE = "2026-10-10T04:00:00+08:00"

CARD_MARKER = "CARD-MARKER-DSH-DEV-7a1f"
PROJECT_MARKER = "PROJECT-RULE-MARKER-DSH-DEV-7a1f"
LEAK_MARKER = "CONTEXT-SECRET-MARKER-dsh-dev-9d31f2"

PROJECT_RULES_TEXT = (
    "# 合成项目规则（离线夹具）\n\n"
    "- " + PROJECT_MARKER + "：本行不得出现在任何错误详情里。\n"
)
CARD_TEXT = (
    "---\n"
    "cardId: \"D4-DSH-DEVELOPMENT-ADAPTER-OFFLINE-001\"\n"
    "cardRevision: 2\n"
    "---\n\n"
    "# 合成任务卡（离线夹具）\n\n"
    "- " + CARD_MARKER + "：仅用于离线断言。\n"
)
CARD_B_TEXT = (
    "---\n"
    "cardId: \"D4-DSH-DEVELOPMENT-ADAPTER-OFFLINE-001-B\"\n"
    "cardRevision: 3\n"
    "---\n\n"
    "# 合成任务卡 B（内容不同 → contextDigest 不同）\n"
)
LEAKY_CARD_TEXT = (
    "---\n"
    "cardId: \"leaky\"\n"
    "cardRevision: 1\n"
    "---\n\n"
    "# 合成任务卡\n\n- " + LEAK_MARKER + "\n"
)

#: acceptance[11]：冻结依赖 SHA；漂移即失败停止。
FROZEN_DEPENDENCIES = (
    ("orchestrator/dsh_launch.py",
     "2554fd1047badf00909526c68ff875906222236811fe1e585ae5d4444e477fea"),
    ("contracts/development_context.py",
     "ef3cacc46819a15541c94813af81f9e6ca8d8a6099fff3ca9371d02d37539c03"),
    ("service/dsh_member_preflight.py",
     "7bb3dddd3366aa460c3229dd27f566c6ddac011ae75ee189bf956e30d3a14091"),
)

#: 离线版本桩：`--version` 输出支持基线，任务启动分支永不执行。
VERSION_STUB = '''\
import os, sys
args = sys.argv[1:]
if "--version" in args:
    sys.stdout.write(os.environ.get("DSH_STUB_VERSION", "0.2.0-rc.2") + "\\n")
    sys.exit(int(os.environ.get("DSH_STUB_VERSION_EXIT", "0")))
sys.stderr.write("TASK_SDK_LAUNCHED\\n")
sys.exit(3)
'''

CEILING = {
    "writeScope": [ROOT],
    "allowNetwork": False,
    "allowBusinessWrite": False,
    "allowGit": False,
    "allowPublish": False,
}
NARROW_CEILING = {
    "writeScope": [NOT_COVERING_ROOT],
    "allowNetwork": False,
    "allowBusinessWrite": False,
    "allowGit": False,
    "allowPublish": False,
}


def _raw(value):
    """fixture 用：字符串按原样写入（便于构造坏 JSON），其余按 JSON 编码。"""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_text(text):
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 磁盘 fixture（只落本 attempt receipts 目录）
# ---------------------------------------------------------------------------
def _build_fs_fixture():
    """在 receipts 下新建独立 fixture 目录；已存在即失败，不覆盖任何既有目录。"""
    base = Path(RECEIPT_ROOT)
    base.mkdir(parents=True, exist_ok=True)
    root = base / (FIXTURE_PREFIX + "%d-%s" % (os.getpid(), uuid.uuid4().hex[:10]))
    os.mkdir(str(root), 0o700)
    workspace = root / "workspace"
    (workspace / "task-cards").mkdir(parents=True, mode=0o700)
    (workspace / "out").mkdir(mode=0o700)
    (workspace / "AGENTS.md").write_text(PROJECT_RULES_TEXT, encoding="utf-8")
    (workspace / "task-cards" / "card.md").write_text(CARD_TEXT, encoding="utf-8")
    (workspace / "task-cards" / "card-b.md").write_text(CARD_B_TEXT, encoding="utf-8")
    (workspace / "task-cards" / "leaky-card.md").write_text(LEAKY_CARD_TEXT, encoding="utf-8")
    (workspace / "inside-entry.js").write_text("// 位于可写 workspace 内的入口\n",
                                              encoding="utf-8")
    runtime = root / "runtime"
    (runtime / "sdk").mkdir(parents=True, mode=0o700)
    node = runtime / "node"
    node.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    os.chmod(str(node), 0o755)
    entry = runtime / "sdk" / "main.js"
    entry.write_text("// 离线夹具入口；从不执行\n", encoding="utf-8")
    os.chmod(str(entry), 0o644)
    (root / "control").mkdir(mode=0o700)
    (root / "store").mkdir(mode=0o700)
    (root / "homes").mkdir(mode=0o700)
    # 用户符号链接（用于“根目录软链接必须拒绝”负例）。
    os.symlink(str(workspace), str(root / "alias-root"))
    return root


def _remove_fs_fixture(root):
    resolved = os.path.realpath(str(root))
    if not resolved.startswith(os.path.realpath(RECEIPT_ROOT) + os.sep) \
            or not os.path.basename(resolved).startswith(FIXTURE_PREFIX):
        raise AssertionError("refusing to remove fixture outside this attempt root: " + resolved)
    if os.path.isdir(resolved):
        shutil.rmtree(resolved)


class _HomeFactory:
    """把隔离器的私有 HOME 落在 fixture 内（不写 /private/tmp，也不写仓库外）。"""

    def __init__(self, base):
        self.base = Path(base)
        self.created = []

    def mkdtemp(self, prefix="tmp", dir=None):
        self.base.mkdir(parents=True, exist_ok=True)
        target = self.base / ("%s%s" % (prefix, uuid.uuid4().hex[:8]))
        os.mkdir(str(target), 0o700)
        self.created.append(str(target))
        return str(target)


# ---------------------------------------------------------------------------
# 离线假 transport（证据标 SYNTHETIC）
# ---------------------------------------------------------------------------
class FakeLegacyTransport:
    """形状对齐 ``orchestrator.dsh.DSHAdapter`` 的离线夹具；不产生任何子进程。

    ``env`` 保留**传入的同一引用**，并记录构造时刻的 env 摘要，用于断言
    “transport 构造之后环境不再修改”。
    """

    TERMINAL = {"failed", "unknown", "awaiting_validation"}

    def __init__(self, workspace, *, submit_receipt=None):
        self.workspace = Path(workspace)
        self.command = [sys.executable, "-c", VERSION_STUB]
        self.env = {}
        self.child = None
        self.submit_receipt = submit_receipt if submit_receipt is not None else {
            "messageId": "msg-dsh-dev-1", "accepted": True,
            "sessionId": "session-dsh-dev-1", "state": "accepted"}
        self.start_calls = 0
        self.submit_calls = []
        self.close_calls = 0
        self.env_digest_at_build = None

    def start(self):
        self.start_calls += 1
        return {"processAlive": True, "protocolReady": True, "owner": True}

    def submit(self, task_id, revision, attempt_id, prompt, instruction_digest):
        self.submit_calls.append({"taskId": task_id, "revision": revision,
                                  "attemptId": attempt_id, "prompt": prompt,
                                  "instructionDigest": instruction_digest})
        return dict(self.submit_receipt)

    def get_status(self, attempt_id):
        return {"state": "awaiting_validation", "endReason": {"kind": "completed"},
                "runtimeClosed": bool(self.close_calls)}

    def close(self):
        self.close_calls += 1


class _RecordingTransportFactory:
    """离线注入的 transport 工厂：记录调用参数并保留 env 同一引用。

    夹具自身的 ``command`` 保持内置版本桩（离线回答 ``--version``）；真实 transport
    使用的 argv 单独记录在 ``calls[*]["command"]`` 与 ``transport.launch_command``。
    """

    def __init__(self, workspace):
        self.workspace = workspace
        self.calls = []
        self.transport = None

    def __call__(self, *, store_dir, command, cwd, env, provider, model):
        self.calls.append({"storeDir": store_dir, "command": list(command), "cwd": cwd,
                           "env": env, "provider": provider, "model": model,
                           "envDigestAtBuild": _digest_text(_canonical(env))})
        transport = FakeLegacyTransport(self.workspace)
        transport.launch_command = list(command)
        transport.env = env
        transport.env_digest_at_build = _digest_text(_canonical(env))
        self.transport = transport
        return transport


# ---------------------------------------------------------------------------
# 内存 registry fixture
# ---------------------------------------------------------------------------
DEFAULT_SPEC = {
    "host_type": "dsh",
    "env_adapter_type": "dsh-sdk",
    "binding_adapter_type": "dsh-sdk",
    "execution_profile_ref": "sdk-minimal",
    "host_agent_id": D4_DSH_SDK_AGENT_ID,
    "model_strategy": "fixed",
    "provider": "synthetic-dsh-provider",
    "model_id": "synthetic-dsh-model",
    "env_allowed_roots": [ROOT],
    "env_ceiling": CEILING,
    "member_allowed_roots": [ROOT],
    "member_ceiling": CEILING,
    "project_ceiling": CEILING,
    "checks": "four",
}

LEGACY_TABLES = ("sources", "agents", "tasks", "attempts", "events", "deliveries", "reviews")
REGISTRY_TABLES = (
    "registry_metadata", "environments", "model_profiles", "registry_agents", "agent_bindings",
    "project_agents", "agent_checks", "attempt_execution_bindings", "registry_changes",
)


def _check_rows(kind):
    if kind == "none":
        return []
    return (("config", None, None), ("identity", None, None),
            ("model", MODEL_ID, 1), ("runtime", None, None))


def _seed_registry(conn, spec):
    conn.execute(
        "INSERT INTO sources (source_id, label, registered_at) VALUES ('src-dsh','合成 DSH 来源',?)",
        (TS,),
    )
    conn.execute(
        "INSERT INTO agents (agent_id, label, note, registered_at) VALUES (?,?,?,?)",
        (MEMBER_ID, "DSH SDK 执行成员", "synthetic registry entry", TS),
    )
    apply_registry_candidate_migration(
        conn, registry_id=REGISTRY_ID, source_digest=SOURCE_DIGEST)
    conn.execute(
        "INSERT INTO environments (environment_id, revision, host_type, instance_id, host_version,"
        " adapter_type, connector_config_ref, config_digest, allowed_roots_json,"
        " permission_ceiling_json, enabled, created_by_json, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (ENV_ID, 1, spec["host_type"], "inst-dsh-1", "0.2.0-rc.2", spec["env_adapter_type"],
         "conn-dsh", DIGEST_A, _raw(spec["env_allowed_roots"]),
         _raw(spec["env_ceiling"]), 1, "{}", TS),
    )
    conn.execute(
        "INSERT INTO model_profiles (model_profile_id, profile_revision, environment_id,"
        " environment_revision, strategy, provider, model_id, config_digest, created_by_json,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (MODEL_ID, 1, ENV_ID, 1, spec["model_strategy"], spec["provider"], spec["model_id"],
         DIGEST_A, "{}", TS),
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO registry_agents (agent_id, agent_alias, normalized_alias, display_name,"
            " roles_json, capabilities_json, environment_id, environment_revision,"
            " allowed_roots_json, permission_ceiling_json, model_profile_id,"
            " model_profile_revision, current_binding_revision, enabled, registration_complete,"
            " revision, source_row_json, created_by_json, created_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (MEMBER_ID, "DSH-SDK-EXEC", "dsh-sdk-exec", "DSH SDK 执行成员",
             '["skill-worker"]', '["develop"]', ENV_ID, 1,
             _raw(spec["member_allowed_roots"]), _raw(spec["member_ceiling"]), MODEL_ID, 1, 1,
             1, 1, 1, None, "{}", TS, TS),
        )
        conn.execute(
            "INSERT INTO agent_bindings (agent_id, binding_revision, environment_id,"
            " environment_revision, adapter_type, connector_config_ref, execution_profile_ref,"
            " host_agent_id, config_digest, created_by_json, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (MEMBER_ID, 1, ENV_ID, 1, spec["binding_adapter_type"], "conn-dsh",
             spec["execution_profile_ref"], spec["host_agent_id"], DIGEST_A, "{}", TS),
        )
        conn.execute("COMMIT")
    except Exception:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        raise
    conn.execute(
        "INSERT INTO project_agents (project_id, agent_id, project_roles_json,"
        " permission_ceiling_json, enabled, revision, created_by_json, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (PROJECT_ID, MEMBER_ID, '["skill-worker"]', _raw(spec["project_ceiling"]), 1, 1,
         "{}", TS, TS),
    )
    for check_type, mp_id, mp_rev in _check_rows(spec["checks"]):
        conn.execute(
            "INSERT INTO agent_checks (check_id, agent_id, binding_revision, environment_id,"
            " environment_revision, config_digest, model_profile_id, model_profile_revision,"
            " check_type, status, evidence_level, observed_at, expires_at, evidence_ref,"
            " failure_reason, requested_json, observed_json, created_by_json)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("chk-dsh-%s" % check_type, MEMBER_ID, 1, ENV_ID, 1, DIGEST_A, mp_id, mp_rev,
             check_type, "passed", "LOCAL", TS, FUTURE,
             "evidence://synthetic/dsh-dev/1", None, '{"synthetic": true}', None, "{}"),
        )


def _dump_tables(conn):
    dump = {"__master__": [tuple(r) for r in conn.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()]}
    for name in LEGACY_TABLES + REGISTRY_TABLES:
        try:
            dump[name] = [tuple(r) for r in conn.execute("SELECT * FROM %s" % name).fetchall()]
        except sqlite3.Error:
            dump[name] = None
    return dump


# ---------------------------------------------------------------------------
# 基类
# ---------------------------------------------------------------------------
_UNSET = object()


class FixtureCase(unittest.TestCase):
    """每类一份磁盘 fixture（只读），每个用例一份内存 registry。"""

    #: 可覆盖的 fixture 规格；值为 callable 时以 fixture root 调用后取值。
    SPEC = {}

    @classmethod
    def setUpClass(cls):
        cls.fixture_root = _build_fs_fixture()

    @classmethod
    def tearDownClass(cls):
        _remove_fs_fixture(cls.fixture_root)

    def setUp(self):
        self.root = self.fixture_root
        self.homes = _HomeFactory(self.root / "homes")
        patcher = mock.patch("orchestrator.isolation.tempfile.mkdtemp",
                             side_effect=self.homes.mkdtemp)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.store = SqliteStore(":memory:")
        self.conn = self.store.conn
        spec = {}
        for key, value in dict(DEFAULT_SPEC, **self.SPEC).items():
            spec[key] = value(self.root) if callable(value) else value
        _seed_registry(self.conn, spec)
        self.addCleanup(self.store.close)

        self.workspace = str(self.root / "workspace")
        self.transport_factory = _RecordingTransportFactory(Path(self.workspace))

    # -- 便捷入口 ---------------------------------------------------------
    def path(self, *parts):
        return str(self.root.joinpath(*parts))

    def card(self, name="card.md"):
        return self.path("workspace", "task-cards", name)

    def digests(self, card=None):
        return {
            "role": "sha256:" + _file_sha256(ROLE_PATH),
            "project": "sha256:" + _file_sha256(self.path("workspace", "AGENTS.md")),
            "taskCard": "sha256:" + _file_sha256(card or self.card()),
        }

    def task(self, **over):
        base = {
            "task_id": "task-dsh-dev-1",
            "project_id": PROJECT_ID,
            "agent_id": MEMBER_ID,
            "source_id": DSH_SDK_SOURCE_ID,
            "workspace": self.workspace,
            "goal": "离线装配 DSH 开发 Adapter 并核对边界。",
            "acceptance_json": '["零 transport 构造即失败"]',
            "permissions_json": _canonical({
                "writeScope": ["out"], "allowNetwork": False,
                "allowBusinessWrite": False, "allowGit": False, "allowPublish": False}),
            "limits_json": _canonical({"maxAttempts": 1, "maxReviewRounds": 1,
                                       "timeoutMinutes": 10}),
            "status": "active",
            "revision": 1,
            "current_attempt_id": "att-dsh-dev-1",
            "current_delivery_id": None,
        }
        base.update(over)
        return base

    def attempt(self, task=None, **over):
        task = task or self.task()
        base = {"attempt_id": task["current_attempt_id"], "task_id": task["task_id"],
                "created_at": TS, "terminal_at": None, "state": "created"}
        base.update(over)
        return base

    def kwargs(self, *, task_over=None, request_over=None, caller=_UNSET, digests=None,
               **over):
        task = self.task(**(task_over or {}))
        if caller is _UNSET:
            caller = owner_context(actor_id="cox", entry="dsh-dev-adapter-test",
                                   synthetic=True)
        values = {
            "conn": self.conn,
            "registry_id": REGISTRY_ID,
            "caller": caller,
            "request": dict({"agentId": MEMBER_ID, "projectId": PROJECT_ID,
                             "modelProfileId": MODEL_ID}, **(request_over or {})),
            "evaluated_at": EVAL_AT,
            "task": task,
            "attempt": self.attempt(task),
            "workspace": self.workspace,
            "role_path": ROLE_PATH,
            "task_card_path": self.card(),
            "allowed_task_roots": [self.path("workspace", "task-cards")],
            "expected_digests": digests if digests is not None else self.digests(),
            "node": self.path("runtime", "node"),
            "sdk_entry": self.path("runtime", "sdk", "main.js"),
            "runtime_roots": [self.path("runtime")],
            "state_dir": self.path("control"),
            "store_dir": self.path("store"),
            "transport_factory": self.transport_factory,
        }
        values.update(over)
        return values

    def call_factory(self, **over):
        return build_development_adapter(**self.kwargs(**over))

    def expect_error(self, **over):
        try:
            self.call_factory(**over)
        except ContractError as exc:
            return exc.code, exc.details
        raise AssertionError("期望受控 ContractError，实际成功返回")

    def assert_zero_transport(self):
        self.assertEqual(self.transport_factory.calls, [], "失败路径不得构造任何 transport")

    # -- 上下文与直接构造适配器（适配器级断言用） --------------------------
    def context(self, card=None, digests=None):
        card_path = card or self.card()
        return development_context_module.build_development_context(
            workspace=self.workspace,
            task_card_path=card_path,
            role_path=ROLE_PATH,
            allowed_task_roots=[self.path("workspace", "task-cards")],
            expected_digests=digests if digests is not None else self.digests(card=card_path),
        )

    def direct_adapter(self, context, store_dir):
        transport = FakeLegacyTransport(Path(self.workspace))
        transport.env = {"HOME": self.path("homes"), "PATH": "/usr/bin:/bin",
                         "LANG": "en_US.UTF-8"}
        return DshDevelopmentAdapter(transport, store_dir, development_context=context)


# =====================================================================
# acceptance[11]：冻结依赖 SHA + 交付物 SHA 记录 + 语法门禁
# =====================================================================
class FrozenDependencyTests(unittest.TestCase):
    def test_dependency_sha256_matches_frozen_value(self):
        for relative, expected in FROZEN_DEPENDENCIES:
            with self.subTest(relative=relative):
                path = os.path.join(WORKSPACE_ROOT, relative)
                self.assertTrue(os.path.isfile(path), "冻结依赖缺失：" + relative)
                actual = _file_sha256(path)
                self.assertEqual(actual, expected,
                                 "冻结依赖 SHA 漂移，停止：%s（期望 %s，实际 %s）"
                                 % (relative, expected, actual))

    def test_new_product_files_exist(self):
        for relative in ("adapters/dsh_development.py", "tests/test_d4_dsh_development.py"):
            self.assertTrue(os.path.isfile(os.path.join(WORKSPACE_ROOT, relative)), relative)


class ArtifactSha256Tests(unittest.TestCase):
    def test_print_delivered_sha256_and_py_compile(self):
        """打印两个交付文件的实测 SHA-256 并做语法门禁（字节码只写临时目录）。"""
        Path(RECEIPT_ROOT).mkdir(parents=True, exist_ok=True)
        scratch = Path(tempfile.mkdtemp(prefix=COMPILE_PREFIX, dir=RECEIPT_ROOT))
        try:
            lines = []
            for relative in ("adapters/dsh_development.py", "tests/test_d4_dsh_development.py"):
                source = os.path.join(WORKSPACE_ROOT, relative)
                py_compile.compile(
                    source,
                    cfile=str(scratch / (relative.replace("/", "_") + "c")),
                    doraise=True)
                lines.append("[D4-DSH-DEV-ADAPTER] %s sha256=%s"
                             % (relative, _file_sha256(source)))
            print("\n".join(lines))
            self.assertEqual(len(lines), 2)
        finally:
            resolved = os.path.realpath(str(scratch))
            if not resolved.startswith(os.path.realpath(RECEIPT_ROOT) + os.sep) \
                    or not os.path.basename(resolved).startswith(COMPILE_PREFIX):
                raise AssertionError("refusing to remove unexpected compile scratch: " + resolved)
            if os.path.isdir(resolved):
                shutil.rmtree(resolved)


# =====================================================================
# 正向路径
# =====================================================================
class HappyPathTests(FixtureCase):
    def test_returns_unstarted_adapter(self):
        adapter = self.call_factory()
        self.assertIsInstance(adapter, DshDevelopmentAdapter)
        self.assertIsInstance(adapter, DshSdkAdapter)
        self.assertTrue(DshDevelopmentAdapter.development_adapter)
        # 工厂只装配：不 start、不 submit、不 close
        self.assertEqual(self.transport_factory.transport.start_calls, 0)
        self.assertEqual(self.transport_factory.transport.submit_calls, [])
        self.assertEqual(self.transport_factory.transport.close_calls, 0)

    def test_transport_receives_launch_command_cwd_and_env(self):
        adapter = self.call_factory()
        call = self.transport_factory.calls[0]
        report = adapter.setup_report
        self.assertEqual(call["command"], report["launch"]["argv"])
        self.assertEqual(list(call["command"][:2]), ["/usr/bin/sandbox-exec", "-p"])
        self.assertEqual(list(call["command"][3:]),
                         [self.path("runtime", "node"),
                          self.path("runtime", "sdk", "main.js"),
                          "--profile", "sdk-minimal"])
        self.assertEqual(call["cwd"], report["launch"]["cwd"])
        self.assertIn("HOME", call["env"])

    def test_provider_and_model_come_from_preflight_profile(self):
        adapter = self.call_factory()
        call = self.transport_factory.calls[0]
        self.assertEqual(call["provider"], "synthetic-dsh-provider")
        self.assertEqual(call["model"], "synthetic-dsh-model")
        # 固定 profile 与预检投影一致（不继承父环境、不自造模型）
        check = dsh_development.preflight_member(
            self.conn, registry_id=REGISTRY_ID,
            caller=owner_context(actor_id="cox", entry="dsh-dev-adapter-test",
                                 synthetic=True),
            request={"agentId": MEMBER_ID, "projectId": PROJECT_ID,
                     "modelProfileId": MODEL_ID},
            evaluated_at=EVAL_AT)
        self.assertEqual(call["provider"], check["member"]["modelProfile"]["provider"])
        self.assertEqual(call["model"], check["member"]["modelProfile"]["modelId"])
        self.assertNotIn("DEEPSEEK_API_KEY", call["env"])
        self.assertIsInstance(adapter, DshDevelopmentAdapter)

    def test_system_prompt_injected_from_context(self):
        adapter = self.call_factory()
        env = self.transport_factory.calls[0]["env"]
        context = self.context()
        self.assertEqual(env["DSH_SYSTEM_PROMPT"], context["systemPrompt"])
        self.assertTrue(env["DSH_SYSTEM_PROMPT"].startswith("# DSH 开发角色说明"))
        self.assertIn("固定权限边界", env["DSH_SYSTEM_PROMPT"])
        self.assertEqual(adapter.setup_report["launch"]["systemPromptSha256"],
                         _digest_text(env["DSH_SYSTEM_PROMPT"]))

    def test_env_is_verified_copy_plus_single_system_prompt_key(self):
        adapter = self.call_factory()
        env = self.transport_factory.calls[0]["env"]
        spec = dsh_development.prepare_dsh_launch(
            node=self.path("runtime", "node"),
            sdk_entry=self.path("runtime", "sdk", "main.js"),
            workspace=self.workspace,
            runtime_roots=[self.path("runtime")],
            state_dir=self.path("control"),
        )
        # 隔离器固定布局保留，且只多出唯一一个键
        self.assertEqual(set(env), set(spec.env) | {"DSH_SYSTEM_PROMPT"})
        for key in ("PATH", "LANG", "PYTHONNOUSERSITE", "PYTHONDONTWRITEBYTECODE"):
            self.assertEqual(env[key], spec.env[key], key)
        self.assertEqual(env["TMPDIR"], env["HOME"] + "/tmp")
        self.assertEqual(env["DSH_HOME"], env["HOME"] + "/dsh")
        self.assertEqual(adapter.setup_report["launch"]["envDigest"],
                         _digest_text(_canonical(env)))

    def test_env_accepts_whitelisted_override_only(self):
        adapter = self.call_factory(env_overrides={"TZ": "Asia/Shanghai"})
        env = self.transport_factory.calls[0]["env"]
        self.assertEqual(env["TZ"], "Asia/Shanghai")
        self.assertIn("DSH_SYSTEM_PROMPT", env)
        self.assertIsInstance(adapter, DshDevelopmentAdapter)

    def test_no_parent_environment_inheritance(self):
        with mock.patch.dict(os.environ, {"D4_DSH_DEV_PARENT_SECRET": "must-not-leak"}):
            self.call_factory()
        env = self.transport_factory.calls[0]["env"]
        self.assertNotIn("D4_DSH_DEV_PARENT_SECRET", env)
        self.assertNotIn("must-not-leak", json.dumps(env))

    def test_env_not_modified_after_transport_construction(self):
        adapter = self.call_factory()
        transport = self.transport_factory.transport
        self.assertIs(transport.env, self.transport_factory.calls[0]["env"])
        self.assertEqual(transport.env_digest_at_build, _digest_text(_canonical(transport.env)))
        self.assertEqual(adapter.setup_report["launch"]["envDigest"],
                         transport.env_digest_at_build)

    def test_call_order_preflight_context_launch_transport(self):
        order = []
        real_preflight = dsh_development.preflight_member
        real_context = dsh_development.build_development_context
        real_launch = dsh_development.prepare_dsh_launch
        base_factory = self.transport_factory

        def spy_preflight(*args, **kwargs):
            order.append("preflight")
            return real_preflight(*args, **kwargs)

        def spy_context(*args, **kwargs):
            order.append("context")
            return real_context(*args, **kwargs)

        def spy_launch(*args, **kwargs):
            order.append("launch")
            return real_launch(*args, **kwargs)

        def spy_factory(**kwargs):
            order.append("transport")
            return base_factory(**kwargs)

        with mock.patch.object(dsh_development, "preflight_member", side_effect=spy_preflight), \
                mock.patch.object(dsh_development, "build_development_context",
                                  side_effect=spy_context), \
                mock.patch.object(dsh_development, "prepare_dsh_launch",
                                  side_effect=spy_launch):
            self.call_factory(transport_factory=spy_factory)
        self.assertEqual(order,
                         ["preflight", "context", "preflight", "launch", "transport"])

    def test_factory_runs_its_own_preflight(self):
        recorded = []
        real = dsh_development.preflight_member

        def spy(conn, **kwargs):
            recorded.append(kwargs)
            return real(conn, **kwargs)

        with mock.patch.object(dsh_development, "preflight_member", side_effect=spy):
            self.call_factory()
        self.assertEqual(len(recorded), 2)
        for kwargs in recorded:
            self.assertEqual(set(kwargs),
                             {"registry_id", "caller", "request", "evaluated_at"})

    def test_payload_cannot_supply_preflight_object(self):
        with self.assertRaises(TypeError):
            self.call_factory(preflight={"decision": "allow"})

    def test_registry_is_read_only_and_no_d4_objects_created(self):
        before_dump = _dump_tables(self.conn)
        before_changes = self.conn.total_changes
        before_factory = self.conn.row_factory
        before_isolation = self.conn.isolation_level
        self.call_factory()
        self.assertEqual(_dump_tables(self.conn), before_dump)
        self.assertEqual(self.conn.total_changes, before_changes)
        self.assertIs(self.conn.row_factory, before_factory)
        self.assertEqual(self.conn.isolation_level, before_isolation)
        self.assertFalse(self.conn.in_transaction)

    def test_environment_limits_read_by_parameterized_select(self):
        seen = []
        self.conn.set_trace_callback(seen.append)
        try:
            self.call_factory()
        finally:
            self.conn.set_trace_callback(None)
        env_selects = [s for s in seen if "FROM environments" in s
                       and "allowed_roots_json" in s]
        self.assertEqual(len(env_selects), 2, "环境上限只应有一次读取 + 一次漂移复核")
        for statement in env_selects:
            self.assertIn("SELECT allowed_roots_json, permission_ceiling_json", statement)
            self.assertIn("WHERE environment_id", statement)
        for statement in seen:
            head = statement.strip().split(" ")[0].upper()
            self.assertIn(head, ("SELECT", "BEGIN", "COMMIT", "ROLLBACK"), statement)

    def test_setup_report_is_honest_synthetic(self):
        adapter = self.call_factory()
        report = adapter.setup_report
        self.assertEqual(report["schemaVersion"], "dsh-development-adapter.v1")
        self.assertEqual(report["sourceId"], DSH_SDK_SOURCE_ID)
        self.assertEqual(report["agentId"], D4_DSH_SDK_AGENT_ID)
        self.assertEqual(report["registryId"], REGISTRY_ID)
        self.assertEqual(report["preflightDecision"], "allow")
        self.assertEqual(report["caller"]["role"], "owner")
        self.assertEqual([item["kind"] for item in report["sources"]],
                         ["role", "project", "taskCard"])
        limits = report["limits"]
        self.assertEqual(limits["evidenceLevel"], "SYNTHETIC")
        self.assertTrue(limits["singleMemberOnly"])
        self.assertTrue(limits["hostAgentIdFromControlledConstant"])
        for key in ("started", "submitted", "createdD4Task", "createdD4Attempt",
                    "createdD4Review", "wouldWrite", "wouldDispatch", "hostStarted",
                    "networkAccessed", "credentialsAccessed", "realDshConnected",
                    "dispatchable"):
            self.assertIs(limits[key], False, key)
        self.assertTrue(limits["realDshAuthorizationGateUnchanged"])

    def test_default_path_constructs_legacy_transport_without_running(self):
        with mock.patch("orchestrator.dsh.subprocess.Popen",
                        side_effect=AssertionError("default path must not spawn")) as popen:
            adapter = self.call_factory(transport_factory=None)
        popen.assert_not_called()
        transport = adapter.transport
        # 测试自己创建的 legacy 实例在用例结束时释放（工厂本身不 close、不 start）。
        self.addCleanup(transport.close)
        self.assertIsInstance(transport, LegacyDshTransport)
        self.assertIsNone(transport.child)
        self.assertFalse(transport.ready)
        self.assertFalse(transport._closed)
        self.assertEqual(transport.command[3:],
                         [self.path("runtime", "node"),
                          self.path("runtime", "sdk", "main.js"),
                          "--profile", "sdk-minimal"])
        self.assertIn("DSH_SYSTEM_PROMPT", transport.env)


# =====================================================================
# acceptance[2]：预检门（deny / recheck / 身份 / 自报）
# =====================================================================
class PreflightGateTests(FixtureCase):
    def test_deny_project_not_associated_zero_transport(self):
        code, details = self.expect_error(request_over={"projectId": "proj-other"})
        self.assertEqual(code, "permission_denied")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_PREFLIGHT_NOT_ALLOWED")
        self.assertEqual(details["decision"], "deny")
        self.assertIn("project_not_associated", details["reasonCodes"])
        self.assert_zero_transport()

    def test_deny_unknown_member_zero_transport(self):
        code, details = self.expect_error(request_over={"agentId": "dev-nobody"})
        self.assertEqual(code, "permission_denied")
        self.assertEqual(details["decision"], "deny")
        self.assertIn("member_not_found", details["reasonCodes"])
        self.assert_zero_transport()

    def test_explicit_null_model_profile_rejected(self):
        code, details = self.expect_error(request_over={"modelProfileId": None})
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "invalid_argument")
        self.assertEqual(details["field"], "request.modelProfileId")
        self.assert_zero_transport()

    def test_worker_caller_denied(self):
        code, details = self.expect_error(
            caller=worker_context(actor_id="dev-1", entry="dsh-dev-adapter-test",
                                  synthetic=True))
        self.assertEqual(code, "permission_denied")
        self.assertEqual(details["role"], "worker")
        self.assert_zero_transport()

    def test_source_caller_denied(self):
        code, details = self.expect_error(
            caller=source_context(source_id="codebuddy-cli", entry="dsh-dev-adapter-test",
                                  synthetic=True))
        self.assertEqual(code, "permission_denied")
        self.assertEqual(details["role"], "source")
        self.assert_zero_transport()

    def test_missing_caller_denied(self):
        code, _details = self.expect_error(caller=None)
        self.assertEqual(code, "permission_denied")
        self.assert_zero_transport()

    def test_self_reported_identity_in_request_denied(self):
        for field in ("role", "caller", "permissionCeiling", "decision", "writeScope"):
            with self.subTest(field=field):
                request_over = {field: "self-reported"}
                code, details = self.expect_error(request_over=request_over)
                self.assertEqual(code, "invalid_task")
                self.assertEqual(details["reason"], "self_reported_identity_not_allowed")
                self.assertEqual(details["field"], "request.%s" % field)
                self.assert_zero_transport()

    def test_unknown_request_field_denied(self):
        code, details = self.expect_error(request_over={"projectRole": "skill-worker"})
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "unknown_request_field")
        self.assert_zero_transport()

    def test_task_attempt_binding_required(self):
        code, details = self.expect_error(attempt={"attempt_id": "att-x", "task_id": "other"})
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_TASK_BINDING_INVALID")
        self.assert_zero_transport()


class RecheckMemberTests(FixtureCase):
    """``recheck``（离线无法确认）同样零 transport 构造。"""

    SPEC = {"checks": "none"}

    def test_recheck_decision_zero_transport(self):
        code, details = self.expect_error()
        self.assertEqual(code, "permission_denied")
        self.assertEqual(details["decision"], "recheck")
        self.assertEqual(details["reasonCodes"], ["check_missing"] * 4)
        self.assert_zero_transport()


# =====================================================================
# acceptance[3]：宿主 / profile / 模型 / hostAgentId / 任务绑定
# =====================================================================
class WrongHostTypeTests(FixtureCase):
    SPEC = {"host_type": "workbuddy"}

    def test_host_type_must_be_exactly_dsh(self):
        code, details = self.expect_error()
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_MEMBER_BINDING_INVALID")
        self.assertEqual(details["field"], "member.environment.hostType")
        self.assert_zero_transport()


class WrongEnvironmentAdapterTypeTests(FixtureCase):
    """环境与 binding 的 adapterType 必须同为 dsh-sdk（否则旧投影先按引用不一致拒绝）。"""

    SPEC = {"env_adapter_type": "workbuddy", "binding_adapter_type": "workbuddy"}

    def test_environment_adapter_type_must_be_dsh_sdk(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "member.environment.adapterType")
        self.assert_zero_transport()


class AdapterTypeReferenceMismatchTests(FixtureCase):
    """环境与 binding 的 adapterType 不一致：复用读取层先按引用不一致拒绝（零 transport）。"""

    SPEC = {"binding_adapter_type": "qclaw"}

    def test_adapter_reference_mismatch_rejected(self):
        code, details = self.expect_error()
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "reference_mismatch")
        self.assert_zero_transport()


class BindingAdapterTypeUnitTests(unittest.TestCase):
    """binding.adapterType 必须精确 dsh-sdk（直接核对校验函数，避免与读取层重复）。"""

    def test_binding_adapter_type_must_be_dsh_sdk(self):
        member = {"environment": {"hostType": "dsh", "adapterType": "dsh-sdk"},
                  "binding": {"adapterType": "qclaw", "executionProfileRef": "sdk-minimal",
                              "hostAgentId": D4_DSH_SDK_AGENT_ID},
                  "modelProfile": {"strategy": "fixed", "provider": "p", "modelId": "m"}}
        with self.assertRaises(ContractError) as ctx:
            dsh_development._assert_member_binding(member)
        self.assertEqual(ctx.exception.details["field"], "member.binding.adapterType")


class WrongExecutionProfileTests(FixtureCase):
    SPEC = {"execution_profile_ref": "sdk-full"}

    def test_execution_profile_must_be_sdk_minimal(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "member.binding.executionProfileRef")
        self.assert_zero_transport()


class WrongHostAgentIdTests(FixtureCase):
    SPEC = {"host_agent_id": "some-other-host-agent"}

    def test_host_agent_id_must_be_controlled_constant(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "member.binding.hostAgentId")
        self.assert_zero_transport()


class HostDefaultModelTests(FixtureCase):
    SPEC = {"model_strategy": "host-default", "provider": None, "model_id": None}

    def test_model_strategy_must_be_fixed(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "member.modelProfile.strategy")
        self.assert_zero_transport()


class TaskBindingTests(FixtureCase):
    def test_task_project_must_match_preflight(self):
        code, details = self.expect_error(task_over={"project_id": "proj-other"})
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_TASK_BINDING_INVALID")
        self.assertEqual(details["field"], "task.project_id")
        self.assert_zero_transport()

    def test_task_agent_must_match_preflight(self):
        code, details = self.expect_error(task_over={"agent_id": "dev-1"})
        self.assertEqual(details["field"], "task.agent_id")
        self.assert_zero_transport()

    def test_task_workspace_must_match_launch_workspace(self):
        other = os.path.realpath(self.path("control"))
        code, details = self.expect_error(task_over={"workspace": other})
        self.assertEqual(details["field"], "task.workspace")
        self.assert_zero_transport()

    def test_fixed_model_fields_must_be_non_empty(self):
        member = {"environment": {"hostType": "dsh", "adapterType": "dsh-sdk"},
                  "binding": {"adapterType": "dsh-sdk", "executionProfileRef": "sdk-minimal",
                              "hostAgentId": D4_DSH_SDK_AGENT_ID},
                  "modelProfile": {"strategy": "fixed", "provider": "", "modelId": "m"}}
        with self.assertRaises(ContractError) as ctx:
            dsh_development._assert_member_binding(member)
        self.assertEqual(ctx.exception.details["field"], "member.modelProfile.provider")

    def test_declared_constants_are_unchanged(self):
        self.assertEqual(dsh_development.REQUIRED_ENVIRONMENT_HOST_TYPE, "dsh")
        self.assertEqual(dsh_development.REQUIRED_ADAPTER_TYPE, DSH_SDK_SOURCE_ID)
        self.assertEqual(dsh_development.REQUIRED_EXECUTION_PROFILE_REF, DSH_SDK_PROFILE)
        self.assertEqual(dsh_development.REQUIRED_HOST_AGENT_ID, D4_DSH_SDK_AGENT_ID)
        self.assertEqual(dsh_development.REQUIRED_MODEL_STRATEGY, "fixed")
        self.assertEqual(D4_DSH_SDK_AGENT_ID, "dsh-sdk-exec-001")


# =====================================================================
# acceptance[4]：allowedRoots / permissionCeiling
# =====================================================================
class MemberRootsTests(FixtureCase):
    SPEC = {"member_allowed_roots": [NOT_COVERING_ROOT]}

    def test_member_allowed_roots_must_cover_workspace(self):
        code, details = self.expect_error()
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_ROOTS_NOT_COVERED")
        self.assertEqual(details["field"], "member.allowedRoots")
        self.assert_zero_transport()


class EnvironmentRootsTests(FixtureCase):
    SPEC = {"env_allowed_roots": [NOT_COVERING_ROOT]}

    def test_environment_allowed_roots_must_cover_workspace(self):
        code, details = self.expect_error()
        self.assertEqual(details["dshDevCode"], "DSH_DEV_ROOTS_NOT_COVERED")
        self.assertEqual(details["field"], "environment.allowedRoots")
        self.assert_zero_transport()


class EnvironmentRootsSymlinkTests(FixtureCase):
    SPEC = {"env_allowed_roots": lambda root: [str(root / "alias-root")]}

    def test_environment_allowed_roots_reject_symlink_segment(self):
        code, details = self.expect_error()
        self.assertEqual(details["dshDevCode"], "DSH_DEV_ROOTS_NOT_COVERED")
        self.assertEqual(details["reason"], "path_symlink_rejected")
        self.assert_zero_transport()


class EnvironmentRootsRelativeTests(FixtureCase):
    SPEC = {"env_allowed_roots": '["relative/root"]'}

    def test_environment_allowed_roots_must_be_absolute(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "environment.allowedRoots[0]")
        self.assert_zero_transport()


class EnvironmentRootsDotdotTests(FixtureCase):
    SPEC = {"env_allowed_roots": '["/private/tmp/../etc"]'}

    def test_environment_allowed_roots_reject_dotdot(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "environment.allowedRoots[0]")
        self.assert_zero_transport()


class EnvironmentRootsShapeTests(FixtureCase):
    SPEC = {"env_allowed_roots": '"not-a-list"'}

    def test_environment_allowed_roots_must_be_non_empty_list(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "environment.allowedRoots")
        self.assertEqual(details["reason"], "roots_not_non_empty_list")
        self.assert_zero_transport()


class EnvironmentCeilingSwitchTests(FixtureCase):
    SPEC = {"env_ceiling": {
        "writeScope": [ROOT], "allowNetwork": True, "allowBusinessWrite": False,
        "allowGit": False, "allowPublish": False}}

    def test_environment_ceiling_high_risk_switch_must_be_false(self):
        code, details = self.expect_error()
        self.assertEqual(details["dshDevCode"], "DSH_DEV_CEILING_NOT_COVERED")
        self.assertEqual(details["field"], "environment.permissionCeiling.allowNetwork")
        self.assert_zero_transport()


class MemberCeilingCoverageTests(FixtureCase):
    SPEC = {"member_ceiling": NARROW_CEILING}

    def test_member_ceiling_must_cover_task_write_scope(self):
        code, details = self.expect_error()
        self.assertEqual(details["dshDevCode"], "DSH_DEV_CEILING_NOT_COVERED")
        self.assertEqual(details["field"], "member.permissionCeiling.writeScope")
        self.assert_zero_transport()


class EnvironmentCeilingCoverageTests(FixtureCase):
    SPEC = {"env_ceiling": NARROW_CEILING}

    def test_environment_ceiling_must_cover_task_write_scope(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "environment.permissionCeiling.writeScope")
        self.assert_zero_transport()


class ProjectCeilingCoverageTests(FixtureCase):
    SPEC = {"project_ceiling": NARROW_CEILING}

    def test_project_ceiling_must_cover_task_write_scope(self):
        code, details = self.expect_error()
        self.assertEqual(details["field"], "project.permissionCeiling.writeScope")
        self.assert_zero_transport()


class TaskPermissionTests(FixtureCase):
    def _permissions(self, **over):
        base = {"writeScope": ["out"], "allowNetwork": False,
                "allowBusinessWrite": False, "allowGit": False, "allowPublish": False}
        base.update(over)
        return _canonical(base)

    def test_task_high_risk_switch_true_rejected(self):
        for key in ("allowNetwork", "allowBusinessWrite", "allowGit", "allowPublish"):
            with self.subTest(key=key):
                permissions = self._permissions(**{key: True})
                code, details = self.expect_error(
                    task_over={"permissions_json": permissions})
                self.assertEqual(details["field"], "task.permissions.%s" % key)
                self.assertEqual(details["reason"], "high_risk_switch_not_explicitly_false")
                self.assert_zero_transport()

    def test_task_missing_high_risk_switch_rejected(self):
        permissions = json.dumps({"writeScope": ["out"], "allowNetwork": False})
        code, details = self.expect_error(task_over={"permissions_json": permissions})
        self.assertEqual(details["reason"], "permission_field_missing")
        self.assert_zero_transport()

    def test_task_write_scope_dotdot_rejected(self):
        code, details = self.expect_error(
            task_over={"permissions_json": self._permissions(writeScope=["../escape"])})
        self.assertEqual(details["reason"], "write_scope_dotdot_segment")
        self.assert_zero_transport()

    def test_task_write_scope_relative_outside_roots_rejected(self):
        code, details = self.expect_error(
            task_over={"permissions_json": self._permissions(writeScope=["/private/tmp/out"])})
        self.assertEqual(details["field"], "member.allowedRoots")
        self.assert_zero_transport()

    def test_task_permissions_json_must_be_object(self):
        code, details = self.expect_error(task_over={"permissions_json": '["out"]'})
        self.assertEqual(details["field"], "task.permissions_json")
        self.assert_zero_transport()

    def test_relative_write_scope_resolves_inside_workspace(self):
        adapter = self.call_factory(
            task_over={"permissions_json": self._permissions(writeScope=["out"])})
        self.assertIsInstance(adapter, DshDevelopmentAdapter)


# =====================================================================
# acceptance[5][6]：上下文与启动规格
# =====================================================================
class ContextTests(FixtureCase):
    def test_digest_drift_rejected_zero_transport(self):
        drifted = dict(self.digests())
        drifted["taskCard"] = "sha256:" + _file_sha256(self.card("card-b.md"))
        code, details = self.expect_error(digests=drifted)
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["reason"], "context_digest_mismatch")
        self.assertEqual(details["field"], "expected_digests.taskCard")
        self.assert_zero_transport()

    def test_role_path_must_be_central(self):
        code, details = self.expect_error(role_path=self.path("workspace", "AGENTS.md"))
        self.assertEqual(details["reason"], "context_path_rejected")
        self.assertEqual(details["field"], "role_path")
        self.assert_zero_transport()

    def test_task_card_outside_roots_rejected(self):
        code, details = self.expect_error(task_card_path=self.path("workspace", "AGENTS.md"))
        self.assertEqual(details["reason"], "context_path_rejected")
        self.assert_zero_transport()

    def test_error_details_do_not_echo_file_body(self):
        leaky = self.card("leaky-card.md")
        digests = dict(self.digests())
        digests["taskCard"] = "sha256:" + _file_sha256(self.card("card-b.md"))
        try:
            self.call_factory(task_card_path=leaky, digests=digests)
        except ContractError as exc:
            self.assertNotIn(LEAK_MARKER, str(exc))
            self.assertNotIn(LEAK_MARKER, json.dumps(exc.details, ensure_ascii=False))
        else:
            raise AssertionError("期望受控 ContractError，实际成功返回")
        self.assert_zero_transport()

    def test_context_failure_skips_launch_and_transport(self):
        real_launch = dsh_development.prepare_dsh_launch
        calls = []

        def spy_launch(*args, **kwargs):
            calls.append(kwargs)
            return real_launch(*args, **kwargs)

        drifted = dict(self.digests())
        drifted["taskCard"] = "sha256:" + _file_sha256(self.card("card-b.md"))
        with mock.patch.object(dsh_development, "prepare_dsh_launch", side_effect=spy_launch):
            self.expect_error(digests=drifted)
        self.assertEqual(calls, [])
        self.assert_zero_transport()


class LaunchTests(FixtureCase):
    def test_credential_env_override_rejected_zero_transport(self):
        with self.assertRaises(IsolationError):
            self.call_factory(env_overrides={"DEEPSEEK_API_KEY": "not-a-real-token"})
        self.assert_zero_transport()

    def test_system_prompt_env_override_rejected(self):
        code, details = self.expect_error(
            env_overrides={"DSH_SYSTEM_PROMPT": "tampered"})
        self.assertEqual(code, "invalid_task")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_ENV_CONFLICT")
        self.assert_zero_transport()

    def test_launch_env_whitelist_kept(self):
        with self.assertRaises(IsolationError):
            self.call_factory(env_overrides={"PATH": "/tmp/attacker"})
        self.assert_zero_transport()

    def test_sdk_entry_inside_workspace_rejected_zero_transport(self):
        with self.assertRaises(IsolationError):
            self.call_factory(sdk_entry=self.path("workspace", "inside-entry.js"))
        self.assert_zero_transport()

    def test_relative_workspace_rejected_zero_transport(self):
        code, details = self.expect_error(workspace="relative/ws")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_ROOTS_NOT_COVERED")
        self.assertEqual(details["field"], "workspace")
        self.assert_zero_transport()


# =====================================================================
# acceptance[10]：构造前并发漂移复核
# =====================================================================
class DriftTests(FixtureCase):
    def _mutating_context(self, mutate):
        real_build = dsh_development.build_development_context

        def side_effect(*args, **kwargs):
            result = real_build(*args, **kwargs)
            mutate()
            return result

        return mock.patch.object(dsh_development, "build_development_context",
                                 side_effect=side_effect)

    def test_member_drift_rejected_zero_transport(self):
        def mutate():
            self.conn.execute(
                "UPDATE registry_agents SET revision = 2 WHERE agent_id = ?", (MEMBER_ID,))

        with self._mutating_context(mutate):
            code, details = self.expect_error()
        self.assertEqual(code, "invalid_transition")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_SOURCE_DRIFT")
        self.assert_zero_transport()
        # 不刷新来源：漂移值原样保留
        self.assertEqual(
            self.conn.execute("SELECT revision FROM registry_agents WHERE agent_id = ?",
                              (MEMBER_ID,)).fetchone()[0], 2)

    def test_project_ceiling_drift_rejected_zero_transport(self):
        def mutate():
            self.conn.execute(
                "UPDATE project_agents SET permission_ceiling_json = ? WHERE project_id = ?"
                " AND agent_id = ?",
                (json.dumps(NARROW_CEILING), PROJECT_ID, MEMBER_ID))

        with self._mutating_context(mutate):
            code, details = self.expect_error()
        self.assertEqual(code, "invalid_transition")
        self.assertEqual(details["dshDevCode"], "DSH_DEV_SOURCE_DRIFT")
        self.assert_zero_transport()

    def test_drift_check_makes_no_write(self):
        before_changes = self.conn.total_changes

        def mutate():
            self.conn.execute(
                "UPDATE registry_agents SET revision = 2 WHERE agent_id = ?", (MEMBER_ID,))

        with self._mutating_context(mutate):
            self.expect_error()
        # 只有测试自己的那一条 UPDATE；工厂自身零写入
        self.assertEqual(self.conn.total_changes, before_changes + 1)
        self.assertFalse(self.conn.in_transaction)

    def test_stable_source_passes_recheck(self):
        adapter = self.call_factory()
        self.assertEqual(adapter.setup_report["preflightDecision"], "allow")


# =====================================================================
# acceptance[7]：prompt / 摘要 / 幂等 / 冻结
# =====================================================================
class PromptAndIdempotencyTests(FixtureCase):
    def test_prompt_carries_full_sources_and_digest(self):
        context = self.context()
        adapter = self.direct_adapter(context, self.path("adapter-store"))
        task, attempt = self.task(), self.attempt()
        prompt = adapter._build_prompt(task, attempt)
        self.assertIn("taskId: %s" % task["task_id"], prompt)
        self.assertIn("attemptId: %s" % attempt["attempt_id"], prompt)
        self.assertIn("0.2.0-rc.2", prompt)
        self.assertIn(DSH_SDK_SOURCE_ID, prompt)
        self.assertIn(context["contextDigest"], prompt)
        for item in context["sources"]:
            self.assertIn(item["sha256"], prompt)
        self.assertIn(PROJECT_MARKER, prompt)
        self.assertIn(CARD_MARKER, prompt)
        self.assertIn(_canonical(context), prompt)
        self.assertEqual(adapter.source_shas,
                         {item["kind"]: item["sha256"] for item in context["sources"]})
        self.assertEqual(adapter.context_digest, context["contextDigest"])

    def test_prompt_block_is_deterministic(self):
        context = self.context()
        adapter = self.direct_adapter(context, self.path("adapter-store"))
        task, attempt = self.task(), self.attempt()
        self.assertEqual(adapter._build_prompt(task, attempt),
                         adapter._build_prompt(task, attempt))
        self.assertEqual(adapter.prompt_context_block(), adapter.prompt_context_block())

    def test_context_is_frozen_at_construction(self):
        context = self.context()
        adapter = self.direct_adapter(context, self.path("adapter-store"))
        task, attempt = self.task(), self.attempt()
        before = adapter._build_prompt(task, attempt)
        context["systemPrompt"] = "TAMPERED-SYSTEM-PROMPT"
        context["contextDigest"] = "sha256:" + "0" * 64
        context["taskContext"]["projectRules"]["text"] = "TAMPERED-RULES"
        context["sources"][0]["sha256"] = "sha256:" + "1" * 64
        after = adapter._build_prompt(task, attempt)
        self.assertEqual(before, after)
        self.assertNotIn("TAMPERED", after)

    def test_same_context_same_attempt_replays(self):
        context = self.context()
        store_dir = self.path("adapter-store")
        task, attempt = self.task(), self.attempt()
        first = self.direct_adapter(context, store_dir)
        result = first.submit(task, attempt)
        self.assertEqual(result.status, "accepted_by_host")
        self.assertEqual(result.host_receipt, "msg-dsh-dev-1")
        second = self.direct_adapter(self.context(), store_dir)
        replayed = second.submit(task, attempt)
        self.assertEqual(second.transport.submit_calls, [], "同内容重投必须回放，不重投")
        self.assertEqual(replayed.status, result.status)
        self.assertEqual(replayed.host_receipt, result.host_receipt)

    def test_different_context_same_attempt_conflicts_and_keeps_ledger(self):
        store_dir = self.path("adapter-store")
        task, attempt = self.task(), self.attempt()
        first = self.direct_adapter(self.context(), store_dir)
        self.assertEqual(first.submit(task, attempt).status, "accepted_by_host")
        ledger = Path(store_dir) / "submissions" / ("%s.json" % attempt["attempt_id"])
        before = ledger.read_bytes()
        second = self.direct_adapter(self.context(card=self.card("card-b.md")), store_dir)
        with self.assertRaises(ContractError) as ctx:
            second.submit(task, attempt)
        self.assertEqual(ctx.exception.code, "invalid_transition")
        self.assertEqual(ctx.exception.details["dshCode"], DSH_SDK_ATTEMPT_CONFLICT)
        self.assertEqual(second.transport.submit_calls, [])
        self.assertEqual(ledger.read_bytes(), before, "旧账本不得被覆盖")

    def test_factory_adapter_submits_with_full_context_prompt(self):
        adapter = self.call_factory()
        task, attempt = self.task(), self.attempt()
        result = adapter.submit(task, attempt)
        self.assertEqual(result.status, "accepted_by_host")
        prompt = self.transport_factory.transport.submit_calls[0]["prompt"]
        self.assertIn(adapter.setup_report["contextDigest"], prompt)
        self.assertIn(CARD_MARKER, prompt)
        self.assertIn(PROJECT_MARKER, prompt)

    def test_setup_report_matches_context_sources(self):
        adapter = self.call_factory()
        context = self.context()
        self.assertEqual(adapter.setup_report["contextDigest"], context["contextDigest"])
        self.assertEqual([item["sha256"] for item in adapter.setup_report["sources"]],
                         [item["sha256"] for item in context["sources"]])


# =====================================================================
# acceptance[8]：不启动 / 不投递 / 不动授权门
# =====================================================================
class BoundarySourceTests(unittest.TestCase):
    """源码级边界：工厂模块不引入进程 / 联网 / 授权门入口。"""

    def _source(self):
        return Path(os.path.join(WORKSPACE_ROOT, "adapters/dsh_development.py")) \
            .read_text(encoding="utf-8")

    def test_no_process_or_network_tokens(self):
        source = self._source()
        for token in ("subprocess", "Popen", "os.system", "socket", "urllib",
                      "requests", "http://", "https://"):
            self.assertNotIn(token, source, "离线工厂不得包含进程/联网入口：" + token)

    def test_does_not_touch_production_authorization_gate(self):
        source = self._source()
        # 不调用授权门、不 import CLI：授权门状态只能由另卡实施，本模块不改动它。
        self.assertNotIn("_dsh_sdk_authorized(", source)
        self.assertNotIn("import scripts", source)
        self.assertNotIn("from scripts", source)
        self.assertNotIn("prod-routing", source)

    def test_never_starts_or_submits(self):
        source = self._source()
        self.assertNotIn(".start()", source)
        self.assertNotIn(".submit(", source)

    def test_environment_select_is_parameterized(self):
        source = self._source()
        self.assertIn("SELECT allowed_roots_json, permission_ceiling_json FROM environments",
                      source)
        self.assertIn("WHERE environment_id = ? AND revision = ?", source)


if __name__ == "__main__":
    unittest.main()
