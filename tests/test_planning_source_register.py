"""D4-MVP20-020 候选：规划来源受控登记的隔离测试（由 cox 独立执行）。

**本文件由 worker 编写但从未运行**（worker 绝不 Bash / 绝不执行测试）；真实 SQLite
隔离测试由 cox 在本 workspace 内独立运行。改动只限于「标准库测试入口 + 固定依赖导入」，
登记业务逻辑与覆盖范围与 019 一致，不含任何 pytest 依赖。

冻结边界：``inputs/cox-register-decision.md``（内容状态：``planned``）。

运行方式（标准库 unittest，无第三方依赖；HERE 为 tests/，WORKSPACE 为其父目录）：

* ``python3 -m unittest discover -s tests -p "test_planning_source_register.py" -v``
* 或直接 ``python3 tests/test_planning_source_register.py -v``

测试纪律：

* 候选模块一律用 ``importlib.util.spec_from_file_location`` 装载（``MODULE.__file__``
  必须指向候选文件本身），禁止意外加载其它同名模块；
* 冻结依赖（``service.context`` / ``service.planning_preflight`` /
  ``storage.planning_schema``）只从冻结绝对目录 ``FORMAL_ROOT``
  （``/Users/demo/.hermes/skills/conductor/shared/agent-orchestrator``）**只读**引用：
  test 端在 import 前把该冻结包根显式插入 ``sys.path[0]``；无环境变量覆盖、无祖先搜索，
  缺依赖即明确失败，测试不修改任何既有源码 / 输入 / 旧证据；
* 临时文件与 SQLite 库只落在本 workspace 的 ``tests/runtime``（或内存），不连接日常库、
  不联网、不起真实服务 / Agent / CLI；
* 每个用例结束关闭自己打开的所有连接；tearDownClass 删除 ``tests/runtime`` 合成产物；
* 本文件只覆盖登记本身：导入、快照、激活、Registry、执行事实均不在范围内。
"""

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import unittest
import uuid
from datetime import datetime
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
WORKSPACE = os.path.dirname(HERE)
CANDIDATE_PATH = os.path.join(WORKSPACE, "service", "planning_source_register.py")
RUNTIME_DIR = os.path.join(HERE, "runtime")

#: 冻结 formal 包根：绝对路径常量。无环境变量覆盖、无祖先搜索、无候选回退；
#: 依赖缺失时立刻明确失败（不换目录、不猜位置）。
FORMAL_ROOT = "/Users/demo/.hermes/skills/conductor/shared/agent-orchestrator"

if not os.path.isfile(os.path.join(FORMAL_ROOT, "service", "context.py")):
    raise RuntimeError(
        "冻结 formal 依赖缺失：%s 下未找到 service/context.py（test 端只固定引用该绝对"
        "路径，不做候选搜索、不回退相对定位）" % FORMAL_ROOT
    )
if FORMAL_ROOT not in sys.path:
    sys.path.insert(0, FORMAL_ROOT)

from service.context import (  # noqa: E402
    CallerContext,
    reviewer_context,
    source_context,
    worker_context,
)
from storage.planning_schema import migrate_planning_schema  # noqa: E402


def _load_candidate():
    """用 spec_from_file_location 装载候选新模块（不靠包名导入）。"""
    assert os.path.isfile(CANDIDATE_PATH), "候选模块不存在：%s" % CANDIDATE_PATH
    spec = importlib.util.spec_from_file_location(
        "candidate_planning_source_register_d4_mvp20_020", CANDIDATE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MODULE = _load_candidate()
CAPABILITY = MODULE.PLANNING_SOURCE_REGISTER
Error = MODULE.PlanningSourceRegistrationError


# ---------------------------------------------------------------------------
# 模块级工具（无状态）
# ---------------------------------------------------------------------------


def _owner_context(actor_id="cox", entry="local-register-cli", synthetic=False):
    return CallerContext(
        actor_id=actor_id,
        role="owner",
        capabilities=frozenset({CAPABILITY}),
        entry=entry,
        synthetic=synthetic,
    )


def _payload(project_id, source_kind, path):
    return {"projectId": project_id, "sourceKind": source_kind, "path": path}


def _write_source(directory, name="product.md", text="# 产品规划来源\n"):
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "wb") as handle:
        handle.write(text.encode("utf-8"))
    return path


def _allow_all_authorizer(action, arg1, arg2, database_name, trigger):
    """显式放行回调：用于解除上一次受限 authorizer（不依赖 ``set_authorizer(None)``）。"""
    return sqlite3.SQLITE_OK


def _rejected_contexts():
    return [
        ("none", None),
        ("plain_dict", {}),
        ("foreign_object", object()),
        ("worker", worker_context()),
        ("reviewer", reviewer_context()),
        ("source", source_context()),
        ("owner_without_capability", CallerContext(
            actor_id="cox", role="owner", capabilities=frozenset(),
            entry="local-register-cli")),
        ("owner_empty_actor", CallerContext(
            actor_id="", role="owner", capabilities=frozenset({CAPABILITY}),
            entry="local-register-cli")),
        ("owner_blank_actor", CallerContext(
            actor_id="   ", role="owner", capabilities=frozenset({CAPABILITY}),
            entry="local-register-cli")),
        ("owner_empty_entry", CallerContext(
            actor_id="cox", role="owner", capabilities=frozenset({CAPABILITY}), entry="")),
        ("owner_unspecified_entry", CallerContext(
            actor_id="cox", role="owner", capabilities=frozenset({CAPABILITY}),
            entry="unspecified")),
    ]


EXTRA_PAYLOAD_FIELDS = (
    {"planningSourceId": "src-我在自己发编号"},
    {"registeredBy": "cox"},
    {"allowedRoot": "/"},
    {"active": True},
    {"actorId": "cox"},
    {"executedAt": "2026-10-10T00:00:00+00:00"},
    {"snapshotId": "snap-1"},
)

MALFORMED_PAYLOADS = (
    {},
    {"projectId": "proj-a"},
    {"projectId": "proj-a", "sourceKind": "product"},
    {"projectId": "", "sourceKind": "product", "path": "/tmp/x.md"},
    {"projectId": "proj-a", "sourceKind": "", "path": "/tmp/x.md"},
    {"projectId": "proj-a", "sourceKind": "product", "path": "   "},
    {"projectId": 1, "sourceKind": "product", "path": "/tmp/x.md"},
    {"projectId": "proj-a", "sourceKind": ["product"], "path": "/tmp/x.md"},
    {"projectId": "proj-a", "sourceKind": "product", "path": None},
    {"projectId": "proj-a", "sourceKind": "Product", "path": "/tmp/x.md"},
    {"projectId": "proj-a", "sourceKind": "planning-source", "path": "/tmp/x.md"},
)

INVALID_ALLOWED_ROOTS = (None, [], "not-a-list", [""], ["relative/root"], [123], ["/tmp", ""])

SENTINEL_TABLES = ("tasks", "attempts", "events", "deliveries", "reviews")


# ---------------------------------------------------------------------------
# 基类：连接追踪 + tests/runtime 合成产物清理
# ---------------------------------------------------------------------------


class RegisterTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # 先清掉上一轮可能残留的合成产物，再建空的 runtime 目录。
        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)
        os.makedirs(RUNTIME_DIR, exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(RUNTIME_DIR, ignore_errors=True)

    def setUp(self):
        self._connections = []

    def tearDown(self):
        for connection in self._connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._connections = []

    # -- 隔离环境工具 -------------------------------------------------------
    def track(self, connection):
        self._connections.append(connection)
        return connection

    def new_runtime_dir(self, name):
        path = os.path.join(RUNTIME_DIR, "%s-%s" % (name, uuid.uuid4().hex))
        os.makedirs(path, exist_ok=True)
        return path

    def new_env(self, name, projects=("proj-a",)):
        """建一个隔离 SQLite 库：既有 workspace_projects + 冻结 planning schema v1。"""
        root = self.new_runtime_dir(name)
        db_path = os.path.join(root, "planning-%s.sqlite3" % uuid.uuid4().hex)
        connection = self.track(sqlite3.connect(db_path))
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "CREATE TABLE workspace_projects (project_id TEXT PRIMARY KEY NOT NULL)"
        )
        for project_id in projects:
            connection.execute(
                "INSERT INTO workspace_projects (project_id) VALUES (?)", (project_id,)
            )
        connection.commit()
        migrate_planning_schema(connection)
        self.assertFalse(connection.in_transaction)
        return connection, db_path, root

    def counts(self, connection):
        return (
            connection.execute("SELECT COUNT(*) FROM planning_sources").fetchone()[0],
            connection.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0],
        )

    def register(self, connection, payload, context=None, allowed_roots=None):
        return MODULE.register_planning_source(
            connection,
            payload,
            context=_owner_context(entry="local-register-cli") if context is None else context,
            allowed_roots=allowed_roots,
        )

    def sentinel_digest(self, connection):
        parts = []
        for table in SENTINEL_TABLES:
            rows = connection.execute(
                "SELECT id, payload FROM %s ORDER BY id" % table
            ).fetchall()
            parts.append(json.dumps([table, rows], ensure_ascii=False, sort_keys=True))
        parts.append(
            json.dumps(
                connection.execute(
                    "SELECT project_id FROM workspace_projects ORDER BY project_id"
                ).fetchall(),
                ensure_ascii=False,
            )
        )
        return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 候选装载与静态边界
# ---------------------------------------------------------------------------


class CandidateBoundaryTest(RegisterTestCase):
    def test_candidate_is_loaded_from_the_candidate_file_only(self):
        self.assertEqual(MODULE.__file__, CANDIDATE_PATH)
        self.assertTrue(
            MODULE.__name__.startswith("candidate_") or "service" in MODULE.__name__
        )

    def test_candidate_never_opens_a_database_or_migrates(self):
        with open(CANDIDATE_PATH, "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("executescript", source)
        self.assertNotIn("sqlite3.connect", source)
        self.assertNotIn("migrate_planning_schema", source)

    def test_non_connection_input_is_rejected_as_storage_not_ready(self):
        with self.assertRaises(Error) as raised:
            self.register(
                "planning.sqlite3",
                _payload("proj-a", "product", "/tmp/x.md"),
                allowed_roots=["/tmp"],
            )
        self.assertEqual(raised.exception.code, "planning_storage_not_ready")


# ---------------------------------------------------------------------------
# 1) 首次登记：来源 + 审计 + 系统编号
# ---------------------------------------------------------------------------


class FirstRegistrationTest(RegisterTestCase):
    def test_first_registration_writes_source_and_audit(self):
        connection, _db, root = self.new_env("first")
        sources_dir = os.path.join(root, "sources")
        source_path = _write_source(sources_dir, "product.md", "产品规划来源 v1\n")
        allowed = [os.path.realpath(root)]
        context = _owner_context(synthetic=True)

        result = self.register(
            connection,
            _payload("proj-a", "product", source_path),
            context=context,
            allowed_roots=allowed,
        )

        # 返回只含登记事实的五个字段；不含可派发 / 在线 / 门禁结论。
        self.assertEqual(
            set(result),
            {"projectId", "planningSourceId", "sourceKind", "path", "replayed"},
        )
        self.assertEqual(result["projectId"], "proj-a")
        self.assertEqual(result["sourceKind"], "product")
        self.assertIs(result["replayed"], False)
        self.assertEqual(result["path"], os.path.realpath(source_path))

        # 编号由系统分配：src- 前缀 + UUID，且不由路径派生。
        self.assertTrue(result["planningSourceId"].startswith("src-"))
        uuid.UUID(hex=result["planningSourceId"][len("src-"):])
        self.assertNotIn(os.path.basename(source_path), result["planningSourceId"])

        rows = connection.execute(
            "SELECT project_id, planning_source_id, source_kind, source_path, allowed_root, "
            "registered_by_json, registered_at FROM planning_sources"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        project_id, source_id, kind, stored_path, stored_root, identity_json, registered_at = rows[0]
        self.assertEqual(project_id, "proj-a")
        self.assertEqual(source_id, result["planningSourceId"])
        self.assertEqual(kind, "product")
        self.assertEqual(stored_path, os.path.realpath(source_path))
        self.assertEqual(stored_root, os.path.realpath(root))
        self.assertEqual(json.loads(identity_json), context.to_dict())
        self.assertIs(json.loads(identity_json)["synthetic"], True)  # synthetic 必须保留
        parsed_at = datetime.fromisoformat(registered_at)
        self.assertIsNotNone(parsed_at.tzinfo)
        self.assertEqual(parsed_at.utcoffset().total_seconds(), 0)

        audits = connection.execute(
            "SELECT audit_id, project_id, planning_source_id, action, actor_json, reason, "
            "occurred_at, detail_json FROM planning_audit"
        ).fetchall()
        self.assertEqual(len(audits), 1)
        audit_id, a_project, a_source, action, actor_json, reason, occurred_at, detail_json = audits[0]
        self.assertTrue(audit_id.startswith("pa-"))
        uuid.UUID(hex=audit_id[len("pa-"):])
        self.assertEqual(a_project, "proj-a")
        self.assertEqual(a_source, result["planningSourceId"])
        self.assertEqual(action, "source.register")
        self.assertEqual(reason, "explicit controlled source registration")
        self.assertEqual(json.loads(actor_json), context.to_dict())
        self.assertEqual(datetime.fromisoformat(occurred_at).utcoffset().total_seconds(), 0)

        detail = json.loads(detail_json)
        with open(source_path, "rb") as handle:
            raw = handle.read()
        self.assertEqual(
            detail["declaredSource"],
            {"projectId": "proj-a", "sourceKind": "product", "path": source_path},
        )
        self.assertEqual(detail["preflightBytes"], len(raw))
        self.assertEqual(detail["preflightSha256"], "sha256:" + hashlib.sha256(raw).hexdigest())

        # 明确不写快照表。
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0], 0
        )

    def test_two_paths_get_two_independent_ids(self):
        connection, _db, root = self.new_env("two-paths")
        allowed = [os.path.realpath(root)]
        first_path = _write_source(os.path.join(root, "a"), "one.md", "one\n")
        second_path = _write_source(os.path.join(root, "b"), "two.md", "two\n")

        first = self.register(connection, _payload("proj-a", "product", first_path), allowed_roots=allowed)
        second = self.register(connection, _payload("proj-a", "product", second_path), allowed_roots=allowed)

        self.assertNotEqual(first["planningSourceId"], second["planningSourceId"])
        self.assertEqual(self.counts(connection), (2, 2))


# ---------------------------------------------------------------------------
# 2) 幂等重放与冲突
# ---------------------------------------------------------------------------


class IdempotencyAndConflictTest(RegisterTestCase):
    def test_duplicate_registration_replays_without_rewriting_identity_or_time(self):
        connection, _db, root = self.new_env("idempotent")
        source_path = _write_source(os.path.join(root, "sources"), "product.md")
        allowed = [os.path.realpath(root)]
        first_context = _owner_context(actor_id="cox", entry="local-register-cli", synthetic=False)
        payload = _payload("proj-a", "product", source_path)

        first = self.register(connection, payload, context=first_context, allowed_roots=allowed)
        snapshot_rows = connection.execute(
            "SELECT planning_source_id, registered_by_json, registered_at FROM planning_sources"
        ).fetchall()
        snapshot_audits = connection.execute("SELECT audit_id FROM planning_audit").fetchall()

        # 第二次用不同身份 / synthetic 重放：必须返回原编号且不改原身份与时间。
        replay_context = _owner_context(actor_id="cox-2", entry="local-register-cli-2", synthetic=True)
        second = self.register(connection, payload, context=replay_context, allowed_roots=allowed)

        self.assertEqual(second["planningSourceId"], first["planningSourceId"])
        self.assertIs(second["replayed"], True)
        self.assertEqual(self.counts(connection), (1, 1))
        self.assertEqual(
            connection.execute(
                "SELECT planning_source_id, registered_by_json, registered_at FROM planning_sources"
            ).fetchall(),
            snapshot_rows,
        )
        self.assertEqual(
            connection.execute("SELECT audit_id FROM planning_audit").fetchall(), snapshot_audits
        )
        self.assertEqual(json.loads(snapshot_rows[0][1])["actorId"], "cox")

    def test_same_path_two_projects_are_isolated(self):
        connection, _db, root = self.new_env("two-projects", projects=("proj-a", "proj-b"))
        source_path = _write_source(os.path.join(root, "sources"), "shared.md")
        allowed = [os.path.realpath(root)]

        first = self.register(connection, _payload("proj-a", "product", source_path), allowed_roots=allowed)
        second = self.register(connection, _payload("proj-b", "product", source_path), allowed_roots=allowed)

        self.assertNotEqual(first["planningSourceId"], second["planningSourceId"])
        self.assertEqual(self.counts(connection), (2, 2))
        # 再加一次重放：只影响对应项目，不新增行。
        again = self.register(connection, _payload("proj-b", "product", source_path), allowed_roots=allowed)
        self.assertEqual(again["planningSourceId"], second["planningSourceId"])
        self.assertEqual(self.counts(connection), (2, 2))

    def test_same_path_different_kind_conflicts_with_zero_write(self):
        connection, _db, root = self.new_env("kind-conflict")
        source_path = _write_source(os.path.join(root, "sources"), "product.md")
        allowed = [os.path.realpath(root)]

        self.register(connection, _payload("proj-a", "product", source_path), allowed_roots=allowed)
        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "development-plan", source_path), allowed_roots=allowed)

        self.assertEqual(raised.exception.code, "planning_source_registration_conflict")
        self.assertEqual(raised.exception.details["registeredSourceKind"], "product")
        self.assertEqual(raised.exception.details["requestedSourceKind"], "development-plan")
        self.assertEqual(self.counts(connection), (1, 1))

    def test_same_path_different_allowed_root_conflicts_with_zero_write(self):
        connection, _db, root = self.new_env("root-conflict")
        deeper = os.path.join(root, "deeper")
        source_path = _write_source(deeper, "plan.md")
        wider = [os.path.realpath(root)]
        narrower = [os.path.realpath(root), os.path.realpath(deeper)]

        first = self.register(connection, _payload("proj-a", "product", source_path), allowed_roots=wider)
        self.assertIs(first["replayed"], False)

        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", source_path), allowed_roots=narrower)

        self.assertEqual(raised.exception.code, "planning_source_registration_conflict")
        self.assertEqual(raised.exception.details["registeredAllowedRoot"], os.path.realpath(root))
        self.assertEqual(raised.exception.details["requestedAllowedRoot"], os.path.realpath(deeper))
        self.assertEqual(self.counts(connection), (1, 1))

    def test_unregistered_project_is_rejected_with_zero_write(self):
        connection, _db, root = self.new_env("unknown-project", projects=("proj-a",))
        source_path = _write_source(os.path.join(root, "sources"), "product.md")
        allowed = [os.path.realpath(root)]

        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-missing", "product", source_path), allowed_roots=allowed)

        self.assertEqual(raised.exception.code, "planning_project_unregistered")
        self.assertEqual(raised.exception.details["projectId"], "proj-missing")
        self.assertEqual(self.counts(connection), (0, 0))


# ---------------------------------------------------------------------------
# 3) payload 形态：未知字段 / 自报编号 / 伪造身份一律拒绝
# ---------------------------------------------------------------------------


class PayloadShapeTest(RegisterTestCase):
    def test_extra_payload_fields_are_rejected_with_zero_write(self):
        for extra in EXTRA_PAYLOAD_FIELDS:
            with self.subTest(extra=extra):
                connection, _db, root = self.new_env("extra-field")
                source_path = _write_source(os.path.join(root, "sources"), "product.md")
                payload = _payload("proj-a", "product", source_path)
                payload.update(extra)

                with self.assertRaises(Error) as raised:
                    self.register(connection, payload, allowed_roots=[os.path.realpath(root)])

                self.assertEqual(raised.exception.code, "planning_source_invalid")
                self.assertEqual(self.counts(connection), (0, 0))

    def test_malformed_payload_is_rejected_with_zero_write(self):
        for payload in MALFORMED_PAYLOADS:
            with self.subTest(payload=payload):
                connection, _db, root = self.new_env("bad-payload")
                with self.assertRaises(Error) as raised:
                    self.register(connection, payload, allowed_roots=[os.path.realpath(root)])
                self.assertEqual(raised.exception.code, "planning_source_invalid")
                self.assertEqual(self.counts(connection), (0, 0))

    def test_non_dict_payload_is_rejected(self):
        connection, _db, root = self.new_env("non-dict-payload")
        with self.assertRaises(Error) as raised:
            self.register(
                connection,
                ["proj-a", "product", "/tmp/x.md"],
                allowed_roots=[os.path.realpath(root)],
            )
        self.assertEqual(raised.exception.code, "planning_source_invalid")

    def test_invalid_allowed_roots_are_rejected(self):
        for allowed_roots in INVALID_ALLOWED_ROOTS:
            with self.subTest(allowed_roots=allowed_roots):
                connection, _db, root = self.new_env("bad-roots")
                source_path = _write_source(os.path.join(root, "sources"), "product.md")
                with self.assertRaises(Error) as raised:
                    self.register(
                        connection,
                        _payload("proj-a", "product", source_path),
                        allowed_roots=allowed_roots,
                    )
                self.assertEqual(raised.exception.code, "planning_source_invalid")
                self.assertEqual(self.counts(connection), (0, 0))


# ---------------------------------------------------------------------------
# 4) 受控授权：拒绝且绝不读取文件
# ---------------------------------------------------------------------------


class AuthorizationTest(RegisterTestCase):
    def test_unauthorized_callers_are_rejected_before_any_file_read(self):
        for name, context in _rejected_contexts():
            with self.subTest(caller=name):
                connection, _db, root = self.new_env("auth")
                source_path = _write_source(os.path.join(root, "sources"), "product.md")
                calls = []

                def _forbidden_preflight(*args, **kwargs):
                    calls.append(args)
                    raise AssertionError("未授权调用不应触发来源预检 / 文件读取")

                with mock.patch.object(MODULE, "preflight_source", _forbidden_preflight):
                    with self.assertRaises(Error) as raised:
                        MODULE.register_planning_source(
                            connection,
                            _payload("proj-a", "product", source_path),
                            context=context,
                            allowed_roots=[os.path.realpath(root)],
                        )

                self.assertEqual(raised.exception.code, "permission_denied")
                self.assertEqual(calls, [])
                self.assertEqual(self.counts(connection), (0, 0))


# ---------------------------------------------------------------------------
# 5) 存储就绪：调用方事务 / 外键 / schema
# ---------------------------------------------------------------------------


class StorageReadinessTest(RegisterTestCase):
    def test_caller_transaction_is_rejected_and_left_untouched(self):
        connection, _db, root = self.new_env("caller-tx")
        source_path = _write_source(os.path.join(root, "sources"), "product.md")
        connection.execute("INSERT INTO workspace_projects (project_id) VALUES ('proj-b')")
        self.assertIs(connection.in_transaction, True)

        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", source_path),
                          allowed_roots=[os.path.realpath(root)])

        self.assertEqual(raised.exception.code, "planning_storage_not_ready")
        self.assertEqual(raised.exception.details["reason"], "caller_transaction_open")
        self.assertIs(connection.in_transaction, True)  # 本模块不代调用方 commit / rollback
        self.assertEqual(
            connection.execute(
                "SELECT COUNT(*) FROM workspace_projects WHERE project_id = 'proj-b'"
            ).fetchone()[0],
            1,
        )
        connection.rollback()
        self.assertIs(connection.in_transaction, False)
        self.assertEqual(self.counts(connection), (0, 0))

    def test_foreign_keys_off_is_rejected(self):
        connection, db_path, root = self.new_env("fk-off")
        source_path = _write_source(os.path.join(root, "sources"), "product.md")
        connection.close()

        reopened = self.track(sqlite3.connect(db_path))  # 新连接默认 foreign_keys=OFF
        self.assertEqual(reopened.execute("PRAGMA foreign_keys").fetchone()[0], 0)
        with self.assertRaises(Error) as raised:
            self.register(reopened, _payload("proj-a", "product", source_path),
                          allowed_roots=[os.path.realpath(root)])
        self.assertEqual(raised.exception.code, "planning_storage_not_ready")
        self.assertEqual(raised.exception.details["reason"], "foreign_keys_off")
        self.assertEqual(self.counts(reopened), (0, 0))

    def test_missing_planning_schema_is_rejected(self):
        root = self.new_runtime_dir("no-schema")
        db_path = os.path.join(root, "bare.sqlite3")
        source_path = _write_source(os.path.join(root, "sources"), "product.md")
        connection = self.track(sqlite3.connect(db_path))
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("CREATE TABLE workspace_projects (project_id TEXT PRIMARY KEY NOT NULL)")
        connection.execute("INSERT INTO workspace_projects (project_id) VALUES ('proj-a')")
        connection.commit()

        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", source_path),
                          allowed_roots=[os.path.realpath(root)])

        self.assertEqual(raised.exception.code, "planning_storage_not_ready")
        self.assertEqual(raised.exception.details["reason"], "planning_schema_absent")
        self.assertIn("planning_sources", raised.exception.details["missing"])


# ---------------------------------------------------------------------------
# 6) 继承预检结论：只 read 可登记，其余保留 diagnostic 且零写入
# ---------------------------------------------------------------------------


class PreflightInheritanceTest(RegisterTestCase):
    def test_relative_path_is_rejected_by_preflight(self):
        connection, _db, root = self.new_env("relative")
        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", "sources/product.md"),
                          allowed_roots=[os.path.realpath(root)])
        self.assertEqual(raised.exception.code, "planning_source_preflight_rejected")
        self.assertEqual(raised.exception.details["status"], "rejected")
        self.assertEqual(raised.exception.details["reason"], "path_not_absolute")
        self.assertEqual(self.counts(connection), (0, 0))

    def test_dotdot_escape_is_rejected_by_preflight(self):
        connection, _db, root = self.new_env("dotdot")
        escaped = os.path.join(root, "sources", "..", "..", "outside.md")
        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", escaped),
                          allowed_roots=[os.path.realpath(root)])
        self.assertEqual(raised.exception.code, "planning_source_preflight_rejected")
        self.assertEqual(raised.exception.details["status"], "rejected")
        self.assertEqual(raised.exception.details["reason"], "path_dotdot_rejected")
        self.assertEqual(self.counts(connection), (0, 0))

    def test_out_of_scope_path_is_rejected_by_preflight(self):
        connection, _db, root = self.new_env("out-of-scope")
        other_root = self.new_runtime_dir("out-of-scope-other")
        outside = _write_source(os.path.join(other_root, "sources"), "product.md")

        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", outside),
                          allowed_roots=[os.path.realpath(root)])

        self.assertEqual(raised.exception.code, "planning_source_preflight_rejected")
        self.assertEqual(raised.exception.details["status"], "out_of_scope")
        self.assertEqual(raised.exception.details["reason"], "path_escape")
        self.assertEqual(self.counts(connection), (0, 0))

    def test_symlink_source_is_rejected_by_preflight(self):
        connection, _db, root = self.new_env("symlink")
        sources_dir = os.path.join(root, "sources")
        target = _write_source(sources_dir, "real.md")
        link = os.path.join(sources_dir, "link.md")
        if os.path.exists(link):
            os.remove(link)
        os.symlink(target, link)

        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", link),
                          allowed_roots=[os.path.realpath(root)])

        self.assertEqual(raised.exception.code, "planning_source_preflight_rejected")
        self.assertEqual(raised.exception.details["status"], "rejected")
        self.assertEqual(raised.exception.details["reason"], "path_symlink_rejected")
        self.assertEqual(self.counts(connection), (0, 0))

    def test_unreadable_source_is_rejected_by_preflight(self):
        connection, _db, root = self.new_env("unreadable")
        missing = os.path.join(root, "sources", "missing.md")
        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", missing),
                          allowed_roots=[os.path.realpath(root)])
        self.assertEqual(raised.exception.code, "planning_source_preflight_rejected")
        self.assertEqual(raised.exception.details["status"], "unreadable")
        self.assertEqual(raised.exception.details["reason"], "document_not_file")
        self.assertEqual(self.counts(connection), (0, 0))

    def test_credential_like_source_is_rejected_by_preflight(self):
        connection, _db, root = self.new_env("credential")
        denied = os.path.join(root, ".ssh", "id_rsa")
        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", denied),
                          allowed_roots=[os.path.realpath(root)])
        self.assertEqual(raised.exception.code, "planning_source_preflight_rejected")
        self.assertEqual(raised.exception.details["status"], "rejected")
        self.assertEqual(raised.exception.details["reason"], "credential_or_private_source_denied")
        self.assertEqual(self.counts(connection), (0, 0))

    def test_template_placeholder_is_not_registrable(self):
        connection, _db, root = self.new_env("placeholder")
        with self.assertRaises(Error) as raised:
            self.register(connection, _payload("proj-a", "product", "<planning-source>"),
                          allowed_roots=[os.path.realpath(root)])
        self.assertEqual(raised.exception.code, "planning_source_preflight_rejected")
        self.assertEqual(raised.exception.details["status"], "pending")
        self.assertEqual(raised.exception.details["reason"], "template_placeholder")
        self.assertEqual(self.counts(connection), (0, 0))


# ---------------------------------------------------------------------------
# 7) 审计失败原子回滚（SQLite authorizer 拒绝 audit insert）
# ---------------------------------------------------------------------------


class AuditRollbackTest(RegisterTestCase):
    def test_audit_insert_denied_rolls_back_the_source_row(self):
        connection, _db, root = self.new_env("audit-deny")
        source_path = _write_source(os.path.join(root, "sources"), "product.md")

        def _deny_audit_insert(action, arg1, arg2, database_name, trigger):
            if action == sqlite3.SQLITE_INSERT and arg1 == "planning_audit":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(_deny_audit_insert)
        try:
            with self.assertRaises(Error) as raised:
                self.register(connection, _payload("proj-a", "product", source_path),
                              allowed_roots=[os.path.realpath(root)])
            self.assertEqual(raised.exception.code, "planning_source_registration_failed")
        finally:
            # 用显式 SQLITE_OK 回调解除（不依赖 set_authorizer(None) 的复位行为）。
            connection.set_authorizer(_allow_all_authorizer)

        # 审计写入被拒 -> 整个事务回滚，零来源残留。
        self.assertEqual(self.counts(connection), (0, 0))
        self.assertIs(connection.in_transaction, False)

        # 解除后同一连接可再次正常读取（证明回调确实复位）。
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_sources").fetchone()[0], 0
        )


# ---------------------------------------------------------------------------
# 8) 重启读回与执行表哨兵
# ---------------------------------------------------------------------------


class RestartAndSentinelTest(RegisterTestCase):
    def test_registration_survives_reopen_and_replays_after_restart(self):
        connection, db_path, root = self.new_env("restart")
        source_path = _write_source(os.path.join(root, "sources"), "product.md")
        allowed = [os.path.realpath(root)]
        first = self.register(connection, _payload("proj-a", "product", source_path), allowed_roots=allowed)
        connection.close()

        reopened = self.track(sqlite3.connect(db_path))
        reopened.execute("PRAGMA foreign_keys = ON")
        rows = reopened.execute(
            "SELECT planning_source_id, source_path, source_kind FROM planning_sources"
        ).fetchall()
        self.assertEqual(
            rows,
            [(first["planningSourceId"], os.path.realpath(source_path), "product")],
        )
        self.assertEqual(
            reopened.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0], 1
        )

        replay = self.register(reopened, _payload("proj-a", "product", source_path), allowed_roots=allowed)
        self.assertEqual(replay["planningSourceId"], first["planningSourceId"])
        self.assertIs(replay["replayed"], True)
        self.assertEqual(self.counts(reopened), (1, 1))

    def test_execution_tables_and_snapshots_are_untouched(self):
        connection, _db, root = self.new_env("sentinels")
        for table in SENTINEL_TABLES:
            connection.execute(
                "CREATE TABLE %s (id TEXT PRIMARY KEY NOT NULL, payload TEXT NOT NULL)" % table
            )
            connection.execute(
                "INSERT INTO %s (id, payload) VALUES (?, ?)" % table, ("seed-1", "sentinel-bytes")
            )
        connection.commit()

        before = self.sentinel_digest(connection)
        snapshots_before = connection.execute(
            "SELECT COUNT(*) FROM planning_snapshots"
        ).fetchone()[0]

        source_path = _write_source(os.path.join(root, "sources"), "product.md")
        self.register(connection, _payload("proj-a", "product", source_path),
                      allowed_roots=[os.path.realpath(root)])

        self.assertEqual(self.sentinel_digest(connection), before)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0],
            snapshots_before,
        )
        self.assertEqual(self.counts(connection), (1, 1))


if __name__ == "__main__":  # pragma: no cover - 标准库 unittest 入口
    unittest.main(verbosity=2)
