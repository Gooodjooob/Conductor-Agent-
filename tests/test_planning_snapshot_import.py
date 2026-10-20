# -*- coding: utf-8 -*-
"""首版受控规划快照保存候选的隔离单元测试。

状态：**NOT_RUN**。Worker 无 bash 权限，本轮未运行任何命令；全部结果由 cox 独立执行
以下入口后判定：

    /usr/bin/python3 -m unittest discover -s tests -p test_planning_snapshot_import.py -v

约定（与 ``inputs/cox-first-import-decision.md`` 一致）：

* 只用标准库 ``unittest`` / ``mock``；不引入 pytest、不安装依赖、不使用 skip。
* 只从冻结的 formal 目录 ``/Users/demo/.hermes/skills/conductor/shared/agent-orchestrator``
  只读导入依赖；候选模块用 ``spec_from_file_location`` 按路径加载，禁止意外导入
  任何同名 formal 模块。
* 候选实现**不调用迁移、不 connect 日常库**；本文件默认口径相同：在隔离临时库
  （内存或 ``tests/runtime`` 下每用例独立目录）里自建镜像 schema，不导入也不调用
  ``storage.planning_schema`` 的迁移入口。
* 024 返工追加的 ``FormalSchemaRootContainmentEndToEndCase`` 是受控例外：它只读冻结
  formal 根，在 ``tests/runtime`` 下每用例独立临时库里调用正式迁移与来源登记入口，
  做根/端到端回归；候选模块自身仍**不 connect、不迁移、不建表**。
* 每个用例只清理自己 ``mkdtemp`` 出来的目录，绝不 rmtree 已存在的未知目录。
* SQLite authorizer 回滚解除用 ``lambda *args: sqlite3.SQLITE_OK``。
"""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from unittest import mock

FORMAL_ROOT = "/Users/demo/.hermes/skills/conductor/shared/agent-orchestrator"
WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATE_PATH = os.path.join(WORKSPACE_ROOT, "service", "planning_snapshot_import.py")
RUNTIME_ROOT = os.path.join(WORKSPACE_ROOT, "tests", "runtime")

if FORMAL_ROOT not in sys.path:
    sys.path.insert(0, FORMAL_ROOT)

from contracts.planning_digest import compute_canonical_digest  # noqa: E402
from contracts.planning_structure import (  # noqa: E402
    validate_planning_structure as formal_validate_structure,
)
from service.context import (  # noqa: E402
    CallerContext,
    owner_context,
    reviewer_context,
    source_context,
    worker_context,
)
from service.planning_preflight import preflight_source as formal_preflight_source  # noqa: E402

_CANDIDATE_NAME = "planning_snapshot_import_candidate"
_SPEC = importlib.util.spec_from_file_location(_CANDIDATE_NAME, CANDIDATE_PATH)
CANDIDATE = importlib.util.module_from_spec(_SPEC)
sys.modules[_CANDIDATE_NAME] = CANDIDATE
_SPEC.loader.exec_module(CANDIDATE)

import_planning_snapshot = CANDIDATE.import_planning_snapshot
PlanningSnapshotImportError = CANDIDATE.PlanningSnapshotImportError
IMPORT_CAPABILITY = CANDIDATE.PLANNING_SNAPSHOT_IMPORT

# ---------------------------------------------------------------------------
# 隔离测试 schema：镜像冻结 planning schema v1（含 CHECK 与 append-only 触发器）
# 仅用于本测试自己的临时库；不导入、不调用 formal 迁移入口。
# ---------------------------------------------------------------------------

_META_DDL = (
    "CREATE TABLE planning_schema_meta (\n"
    "    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\n"
    "    version INTEGER NOT NULL CHECK (version = 1)\n"
    ")"
)
_META_DDL_UNCHECKED = (
    "CREATE TABLE planning_schema_meta (\n"
    "    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\n"
    "    version INTEGER NOT NULL\n"
    ")"
)
_SOURCES_DDL = (
    "CREATE TABLE planning_sources (\n"
    "    project_id TEXT NOT NULL,\n"
    "    planning_source_id TEXT NOT NULL,\n"
    "    source_kind TEXT NOT NULL CHECK (source_kind IN "
    "('product', 'development-plan', 'other-registered')),\n"
    "    source_path TEXT NOT NULL,\n"
    "    allowed_root TEXT NOT NULL,\n"
    "    registered_by_json TEXT NOT NULL,\n"
    "    registered_at TEXT NOT NULL,\n"
    "    PRIMARY KEY (project_id, planning_source_id),\n"
    "    UNIQUE (project_id, source_path),\n"
    "    FOREIGN KEY (project_id) REFERENCES workspace_projects (project_id)\n"
    ")"
)
_SNAPSHOTS_DDL = (
    "CREATE TABLE planning_snapshots (\n"
    "    snapshot_id TEXT PRIMARY KEY NOT NULL,\n"
    "    project_id TEXT NOT NULL,\n"
    "    planning_source_id TEXT NOT NULL,\n"
    "    snapshot_revision INTEGER NOT NULL CHECK "
    "(typeof(snapshot_revision) = 'integer' AND snapshot_revision > 0),\n"
    "    document_version TEXT NOT NULL,\n"
    "    source_sha256 TEXT NOT NULL,\n"
    "    source_bytes INTEGER NOT NULL CHECK "
    "(typeof(source_bytes) = 'integer' AND source_bytes >= 0),\n"
    "    canonical_digest TEXT NOT NULL,\n"
    "    payload_json TEXT NOT NULL,\n"
    "    imported_by_json TEXT NOT NULL,\n"
    "    imported_at TEXT NOT NULL,\n"
    "    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status = 'candidate'),\n"
    "    UNIQUE (project_id, planning_source_id, snapshot_revision),\n"
    "    FOREIGN KEY (project_id, planning_source_id) REFERENCES planning_sources "
    "(project_id, planning_source_id)\n"
    ")"
)
_AUDIT_DDL = (
    "CREATE TABLE planning_audit (\n"
    "    audit_id TEXT PRIMARY KEY NOT NULL,\n"
    "    project_id TEXT NOT NULL,\n"
    "    planning_source_id TEXT NOT NULL,\n"
    "    action TEXT NOT NULL,\n"
    "    actor_json TEXT NOT NULL,\n"
    "    reason TEXT NOT NULL,\n"
    "    occurred_at TEXT NOT NULL,\n"
    "    detail_json TEXT NOT NULL,\n"
    "    FOREIGN KEY (project_id, planning_source_id) REFERENCES planning_sources "
    "(project_id, planning_source_id)\n"
    ")"
)
_TRIGGER_DDL = (
    "CREATE TRIGGER planning_snapshots_reject_update BEFORE UPDATE ON planning_snapshots "
    "BEGIN SELECT RAISE(ABORT, 'planning_snapshots is append-only: UPDATE rejected'); END",
    "CREATE TRIGGER planning_snapshots_reject_delete BEFORE DELETE ON planning_snapshots "
    "BEGIN SELECT RAISE(ABORT, 'planning_snapshots is append-only: DELETE rejected'); END",
    "CREATE TRIGGER planning_audit_reject_update BEFORE UPDATE ON planning_audit "
    "BEGIN SELECT RAISE(ABORT, 'planning_audit is append-only: UPDATE rejected'); END",
    "CREATE TRIGGER planning_audit_reject_delete BEFORE DELETE ON planning_audit "
    "BEGIN SELECT RAISE(ABORT, 'planning_audit is append-only: DELETE rejected'); END",
)

#: 执行账本 / Registry 哨兵表：候选只应写规划快照与审计，绝不碰这些表。
_LEDGER_TABLES = (
    ("tasks", "task_id"),
    ("attempts", "attempt_id"),
    ("events", "event_id"),
    ("deliveries", "delivery_id"),
    ("reviews", "review_id"),
    ("agent_registry", "agent_id"),
)

_STATE_TABLES = (
    "workspace_projects",
    "planning_schema_meta",
    "planning_sources",
    "planning_snapshots",
    "planning_audit",
) + tuple(table for table, _ in _LEDGER_TABLES)

_SOURCE_TEXT = "# 产品规划\n\n## 目标\n\n- 目标一\n"
_MUTATED_SOURCE_TEXT = "# 产品规划\n\n## 目标\n\n- 目标一（已变化）\n"


def _file_meta(path):
    with open(path, "rb") as handle:
        raw = handle.read()
    return {"bytes": len(raw), "sha256": "sha256:" + hashlib.sha256(raw).hexdigest()}


def _table_rows(conn, table):
    try:
        rows = conn.execute("SELECT * FROM %s" % table).fetchall()
    except sqlite3.Error:
        return None
    return sorted(repr(tuple(row)) for row in rows)


def _changed_tables(before, after):
    keys = set(before) | set(after)
    return sorted(key for key in keys if before.get(key) != after.get(key))


class SnapshotImportCase(unittest.TestCase):
    def setUp(self):
        # 决策 §22：每用例独立目录放在本 workspace 的 tests/runtime 下。
        os.makedirs(RUNTIME_ROOT, exist_ok=True)
        self._owned_dirs = []
        self._connections = []

    def tearDown(self):
        for conn in self._connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        # 只删除本用例自己创建的目录，绝不 rmtree 已存在的未知目录。
        for path in self._owned_dirs:
            shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------
    # 资源
    # ------------------------------------------------------------------
    def _case_dir(self):
        path = tempfile.mkdtemp(prefix="case-", dir=RUNTIME_ROOT)
        self._owned_dirs.append(path)
        return path

    def _connect(self, path=":memory:", foreign_keys=True):
        conn = sqlite3.connect(path)
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        self._connections.append(conn)
        return conn

    def _write_source(self, directory, name="planning.md", text=None):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_SOURCE_TEXT if text is None else text)
        return path, _file_meta(path)

    def _create_planning_schema(self, conn, *, meta_version=1, meta_check=True, omit=()):
        statements = []
        if "planning_schema_meta" not in omit:
            statements.append(_META_DDL if meta_check else _META_DDL_UNCHECKED)
        if "planning_sources" not in omit:
            statements.append(_SOURCES_DDL)
        if "planning_snapshots" not in omit:
            statements.append(_SNAPSHOTS_DDL)
        if "planning_audit" not in omit:
            statements.append(_AUDIT_DDL)
        if not omit:
            statements.extend(_TRIGGER_DDL)
        for statement in statements:
            conn.execute(statement)
        if "planning_schema_meta" not in omit:
            conn.execute(
                "INSERT INTO planning_schema_meta (singleton, version) VALUES (1, ?)",
                (meta_version,),
            )
        conn.commit()

    def _new_db(self, *, path=":memory:", foreign_keys=True, meta_version=1,
                meta_check=True, omit=(), ledger=True, projects=("proj-a",)):
        conn = self._connect(path, foreign_keys=foreign_keys)
        conn.execute("CREATE TABLE workspace_projects (project_id TEXT PRIMARY KEY)")
        if ledger:
            for table, key in _LEDGER_TABLES:
                conn.execute(
                    "CREATE TABLE %s (%s TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                    % (table, key)
                )
                conn.execute(
                    "INSERT INTO %s (%s, payload) VALUES (?, ?)" % (table, key),
                    ("sentinel-1", '{"sentinel":true}'),
                )
        self._create_planning_schema(
            conn, meta_version=meta_version, meta_check=meta_check, omit=omit
        )
        for project_id in projects:
            conn.execute(
                "INSERT INTO workspace_projects (project_id) VALUES (?)", (project_id,)
            )
        conn.commit()
        return conn

    def _register_source(self, conn, *, project_id="proj-a", planning_source_id="src-a1",
                         source_kind="product", source_path, allowed_root):
        conn.execute(
            "INSERT INTO planning_sources (project_id, planning_source_id, source_kind, "
            "source_path, allowed_root, registered_by_json, registered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                planning_source_id,
                source_kind,
                source_path,
                allowed_root,
                '{"actorId":"cox","role":"owner","entry":"seed","synthetic":true,'
                '"capabilities":["planning.source.register"]}',
                "2026-10-10T00:00:00+00:00",
            ),
        )
        conn.commit()

    def _insert_existing_snapshot(self, conn, *, canonical_digest, document_version,
                                  project_id="proj-a", planning_source_id="src-a1",
                                  revision=1, source_sha256=None, source_bytes=1):
        snapshot_id = "snap-seeded-" + uuid.uuid4().hex[:8]
        conn.execute(
            "INSERT INTO planning_snapshots (snapshot_id, project_id, planning_source_id, "
            "snapshot_revision, document_version, source_sha256, source_bytes, "
            "canonical_digest, payload_json, imported_by_json, imported_at, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                snapshot_id,
                project_id,
                planning_source_id,
                revision,
                document_version,
                source_sha256 or ("sha256:" + "1" * 64),
                source_bytes,
                canonical_digest,
                '{"seeded":true}',
                '{"actorId":"cox","role":"owner","entry":"seed","synthetic":true,'
                '"capabilities":[]}',
                "2026-10-10T00:00:00+00:00",
                "candidate",
            ),
        )
        conn.commit()
        return snapshot_id

    def _owner_ctx(self, synthetic=True):
        return CallerContext(
            actor_id="cox",
            role="owner",
            capabilities=frozenset({IMPORT_CAPABILITY}),
            entry="local-review-cli",
            synthetic=synthetic,
        )

    def _snapshot(self, *, path, meta=None, project_id="proj-a",
                  planning_source_id="src-a1", source_kind="product", name=None,
                  document_version="v1", plan_status="draft", sections=None,
                  stages=None, business_lines=None, milestones=None, extra=None):
        resolved = dict(meta) if meta is not None else _file_meta(path)
        snapshot = {
            "schemaVersion": "planning-snapshot.v1",
            "projectId": project_id,
            "planningSourceId": planning_source_id,
            "sourceKind": source_kind,
            "sourceFile": {
                "name": os.path.basename(path) if name is None else name,
                "path": path,
                "documentVersion": document_version,
                "sha256": resolved["sha256"],
                "bytes": resolved["bytes"],
            },
            "sections": list(sections or []),
            "stages": list(stages or []),
            "businessLines": list(business_lines or []),
            "milestones": list(milestones or []),
            "planStatus": plan_status,
        }
        if extra:
            snapshot.update(extra)
        return snapshot

    def _state(self, conn):
        return {table: _table_rows(conn, table) for table in _STATE_TABLES}

    def _expect_error(self, code, conn, snapshot, *, context, allowed_roots):
        with self.assertRaises(PlanningSnapshotImportError) as caught:
            import_planning_snapshot(
                conn, snapshot, context=context, allowed_roots=allowed_roots
            )
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    # ------------------------------------------------------------------
    # 首次候选：系统编号 / 身份 / 摘要 / 审计
    # ------------------------------------------------------------------
    def test_first_import_saves_candidate_with_system_identity_and_digest(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)

        snapshot = self._snapshot(path=path, meta=meta)
        snapshot_copy = copy.deepcopy(snapshot)
        roots = [case]
        roots_copy = list(roots)

        result = import_planning_snapshot(
            conn, snapshot, context=self._owner_ctx(), allowed_roots=roots
        )

        self.assertEqual(result["projectId"], "proj-a")
        self.assertEqual(result["planningSourceId"], "src-a1")
        self.assertTrue(result["snapshotId"].startswith("snap-"))
        self.assertEqual(len(result["snapshotId"]), len("snap-") + 32)
        self.assertEqual(result["snapshotId"][len("snap-") + 12], "4")  # UUID4 版本位
        self.assertIs(result["replayed"], False)
        self.assertEqual(result["snapshotRevision"], 1)
        self.assertNotIsInstance(result["snapshotRevision"], bool)
        self.assertEqual(result["status"], "candidate")
        self.assertIs(result["wouldActivate"], False)
        self.assertRegex(result["canonicalDigest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(result["diagnostics"], [])
        self.assertFalse(conn.in_transaction)

        rows = conn.execute(
            "SELECT snapshot_id, project_id, planning_source_id, snapshot_revision, "
            "document_version, source_sha256, source_bytes, canonical_digest, "
            "payload_json, imported_by_json, imported_at, status FROM planning_snapshots"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row[0], result["snapshotId"])
        self.assertEqual(row[1], "proj-a")
        self.assertEqual(row[2], "src-a1")
        self.assertEqual(row[3], 1)
        self.assertEqual(row[4], "v1")
        self.assertEqual(row[5], meta["sha256"])
        self.assertEqual(row[6], meta["bytes"])
        self.assertEqual(row[7], result["canonicalDigest"])
        self.assertEqual(row[11], "candidate")

        imported_at = datetime.fromisoformat(row[10])
        self.assertIsNotNone(imported_at.tzinfo)
        self.assertEqual(imported_at.utcoffset(), timedelta(0))

        identity = json.loads(row[9])
        self.assertEqual(identity["actorId"], "cox")
        self.assertEqual(identity["role"], "owner")
        self.assertEqual(identity["entry"], "local-review-cli")
        self.assertIs(identity["synthetic"], True)
        self.assertIn(IMPORT_CAPABILITY, identity["capabilities"])
        self.assertEqual(
            row[9],
            json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

        payload = json.loads(row[8])
        self.assertEqual(payload["snapshotId"], result["snapshotId"])
        self.assertEqual(payload["snapshotRevision"], 1)
        self.assertEqual(payload["importedAt"], row[10])
        self.assertEqual(payload["importedBy"], identity)
        self.assertEqual(payload["canonicalDigest"], result["canonicalDigest"])
        self.assertEqual(payload["projectId"], "proj-a")
        self.assertEqual(payload["sourceFile"], snapshot["sourceFile"])
        self.assertEqual(payload["planStatus"], "draft")
        self.assertEqual(compute_canonical_digest(payload), result["canonicalDigest"])
        # JSON 编码冻结：UTF-8、键排序、非 ASCII 不转义、无多余空格。
        self.assertEqual(
            row[8],
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

        audits = conn.execute(
            "SELECT audit_id, project_id, planning_source_id, action, actor_json, "
            "reason, occurred_at, detail_json FROM planning_audit"
        ).fetchall()
        self.assertEqual(len(audits), 1)
        audit = audits[0]
        self.assertTrue(audit[0].startswith("pa-"))
        self.assertEqual(len(audit[0]), len("pa-") + 32)
        self.assertEqual(audit[0][len("pa-") + 12], "4")
        self.assertEqual(audit[1], "proj-a")
        self.assertEqual(audit[2], "src-a1")
        self.assertEqual(audit[3], "snapshot.import")
        self.assertEqual(audit[4], row[9])
        self.assertEqual(audit[5], "explicit controlled first snapshot import")
        self.assertEqual(audit[6], row[10])
        detail = json.loads(audit[7])
        self.assertEqual(detail["snapshotId"], result["snapshotId"])
        self.assertEqual(detail["snapshotRevision"], 1)
        self.assertEqual(detail["canonicalDigest"], result["canonicalDigest"])
        self.assertEqual(detail["sourceBytes"], meta["bytes"])
        self.assertEqual(detail["sourceSha256"], meta["sha256"])
        self.assertEqual(detail["diagnosticsCount"], 0)
        self.assertEqual(
            audit[7],
            json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )

        # 调用方对象与入参列表都不被修改。
        self.assertEqual(snapshot, snapshot_copy)
        self.assertEqual(roots, roots_copy)

    # ------------------------------------------------------------------
    # 只读复用冻结 formal 依赖：不扫描父目录、不环境覆盖、不备用实现
    # ------------------------------------------------------------------
    def test_candidate_reuses_frozen_formal_dependencies_only(self):
        self.assertEqual(
            os.path.realpath(CANDIDATE.__file__), os.path.realpath(CANDIDATE_PATH)
        )
        self.assertIs(CANDIDATE.validate_planning_structure, formal_validate_structure)
        self.assertIs(CANDIDATE.preflight_source, formal_preflight_source)
        self.assertIs(CANDIDATE.CallerContext, CallerContext)
        # 旧断言曾要求「formal 处不存在同名文件」，但候选被正式位置接纳后该文件必然存在
        # （正式回归正是从它加载，不改产品 SHA），该断言必然失败。改为核验**实际来源**：
        # 候选模块的 spec.origin / __file__ / sys.modules 绑定，以及直接依赖对象是否确实
        # 来自冻结 formal 根。不对 os.path.exists 打桩、不伪装路径、不跳过。
        candidate_real = os.path.realpath(CANDIDATE_PATH)
        self.assertEqual(CANDIDATE.__spec__.name, _CANDIDATE_NAME)
        self.assertEqual(os.path.realpath(CANDIDATE.__spec__.origin), candidate_real)
        self.assertEqual(os.path.realpath(CANDIDATE.__file__), candidate_real)
        # 候选路径在 sys.modules 中只绑定这一个模块对象：唯一候选对象身份。
        self.assertIs(sys.modules[_CANDIDATE_NAME], CANDIDATE)
        duplicate_bindings = sorted(
            name
            for name, module in list(sys.modules.items())
            if name != _CANDIDATE_NAME
            and getattr(module, "__file__", None)
            and os.path.realpath(module.__file__) == candidate_real
        )
        self.assertEqual(duplicate_bindings, [])

        # 冻结 formal 根确实在导入路径上：依赖声明不是「碰巧同名」。
        formal_root_real = os.path.realpath(FORMAL_ROOT)
        self.assertTrue(
            any(os.path.realpath(entry) == formal_root_real for entry in sys.path)
        )
        # 五个直接依赖模块必须真的是冻结 formal 根下的对应文件（按路径组件核验，
        # 不用字符串前缀，也不依赖 formal 同名候选文件是否存在）。
        for module_name in (
            "contracts.planning_digest",
            "contracts.planning_structure",
            "contracts.schemas",
            "service.context",
            "service.planning_preflight",
        ):
            with self.subTest(formal_dependency=module_name):
                module = sys.modules.get(module_name)
                self.assertIsNotNone(module)
                origin = os.path.realpath(module.__spec__.origin)
                self.assertEqual(origin, os.path.realpath(module.__file__))
                self.assertEqual(
                    os.path.relpath(origin, formal_root_real),
                    os.path.join(*module_name.split(".")) + ".py",
                )

        # 候选实际持有的依赖对象，必须就是这些 formal 模块里的同一个对象。
        formal_structure = sys.modules["contracts.planning_structure"]
        formal_preflight = sys.modules["service.planning_preflight"]
        formal_context = sys.modules["service.context"]
        formal_digest = sys.modules["contracts.planning_digest"]
        formal_schemas = sys.modules["contracts.schemas"]
        self.assertIs(
            CANDIDATE.validate_planning_structure,
            formal_structure.validate_planning_structure,
        )
        self.assertIs(CANDIDATE.preflight_source, formal_preflight.preflight_source)
        self.assertIs(
            CANDIDATE.PlanningPreflightError, formal_preflight.PlanningPreflightError
        )
        self.assertIs(CANDIDATE.CallerContext, formal_context.CallerContext)
        self.assertIs(CANDIDATE.require_context, formal_context.require_context)
        self.assertIs(CANDIDATE.PARTICIPATING_FIELDS, formal_digest.PARTICIPATING_FIELDS)
        self.assertIs(CANDIDATE.PlanningDigestError, formal_digest.PlanningDigestError)
        self.assertIs(CANDIDATE.ContractError, formal_schemas.ContractError)

        source = inspect.getsource(CANDIDATE)
        self.assertNotIn("migrate_planning_schema", source)
        self.assertNotIn("sqlite3.connect", source)
        self.assertNotIn("D4_CONTRACTS_ROOT", source)
        self.assertNotIn("executescript(", source)
        self.assertNotIn("DELETE FROM", source)
        self.assertNotIn("UPDATE planning", source)

    # ------------------------------------------------------------------
    # 重放：无新快照 / 无新审计 / 原身份时间不变
    # ------------------------------------------------------------------
    def test_replay_returns_original_identity_without_new_rows(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        ctx = self._owner_ctx()

        first = import_planning_snapshot(conn, snapshot, context=ctx, allowed_roots=[case])
        rows_before = conn.execute("SELECT * FROM planning_snapshots").fetchall()
        audits_before = conn.execute("SELECT * FROM planning_audit").fetchall()

        second = import_planning_snapshot(conn, snapshot, context=ctx, allowed_roots=[case])

        self.assertIs(second["replayed"], True)
        self.assertEqual(second["snapshotId"], first["snapshotId"])
        self.assertEqual(second["snapshotRevision"], 1)
        self.assertEqual(second["status"], "candidate")
        self.assertIs(second["wouldActivate"], False)
        self.assertEqual(second["canonicalDigest"], first["canonicalDigest"])
        self.assertEqual(second["diagnostics"], [])
        self.assertEqual(
            conn.execute("SELECT * FROM planning_snapshots").fetchall(), rows_before
        )
        self.assertEqual(
            conn.execute("SELECT * FROM planning_audit").fetchall(), audits_before
        )

    # ------------------------------------------------------------------
    # 双项目共用同一物理路径：来源隔离
    # ------------------------------------------------------------------
    def test_two_projects_sharing_one_physical_path_stay_isolated(self):
        case = self._case_dir()
        conn = self._new_db(projects=("proj-a", "proj-b"))
        path, meta = self._write_source(case)
        self._register_source(
            conn, project_id="proj-a", planning_source_id="src-a1",
            source_path=path, allowed_root=case,
        )
        self._register_source(
            conn, project_id="proj-b", planning_source_id="src-b1",
            source_path=path, allowed_root=case,
        )
        snapshot_a = self._snapshot(
            project_id="proj-a", planning_source_id="src-a1", path=path, meta=meta
        )
        snapshot_b = self._snapshot(
            project_id="proj-b", planning_source_id="src-b1", path=path, meta=meta
        )
        ctx = self._owner_ctx()

        result_a = import_planning_snapshot(
            conn, snapshot_a, context=ctx, allowed_roots=[case]
        )
        result_b = import_planning_snapshot(
            conn, snapshot_b, context=ctx, allowed_roots=[case]
        )

        self.assertNotEqual(result_a["snapshotId"], result_b["snapshotId"])
        self.assertIs(result_b["replayed"], False)
        self.assertEqual(result_b["snapshotRevision"], 1)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0], 2
        )
        mapped = dict(
            conn.execute(
                "SELECT project_id || '|' || planning_source_id, snapshot_id "
                "FROM planning_snapshots"
            ).fetchall()
        )
        self.assertEqual(mapped["proj-a|src-a1"], result_a["snapshotId"])
        self.assertEqual(mapped["proj-b|src-b1"], result_b["snapshotId"])

        replay_a = import_planning_snapshot(
            conn, snapshot_a, context=ctx, allowed_roots=[case]
        )
        self.assertIs(replay_a["replayed"], True)
        self.assertEqual(replay_a["snapshotId"], result_a["snapshotId"])

        digests = dict(
            conn.execute(
                "SELECT project_id, canonical_digest FROM planning_snapshots"
            ).fetchall()
        )
        # 内容相同也因受控身份不同而得到不同摘要，两个项目互不覆盖。
        self.assertNotEqual(digests["proj-a"], digests["proj-b"])

    # ------------------------------------------------------------------
    # 未登记项目 / 未登记来源 / 跨项目引用
    # ------------------------------------------------------------------
    def test_unknown_project_cross_project_and_unknown_source_are_rejected(self):
        case = self._case_dir()
        conn = self._new_db(projects=("proj-a", "proj-b"))
        path, meta = self._write_source(case)
        self._register_source(
            conn, project_id="proj-a", planning_source_id="src-a1",
            source_path=path, allowed_root=case,
        )
        ctx = self._owner_ctx()
        before = self._state(conn)

        self._expect_error(
            "planning_project_unregistered", conn,
            self._snapshot(project_id="proj-missing", planning_source_id="src-a1",
                           path=path, meta=meta),
            context=ctx, allowed_roots=[case],
        )
        self._expect_error(
            "planning_source_unregistered", conn,
            self._snapshot(project_id="proj-b", planning_source_id="src-b1",
                           path=path, meta=meta),
            context=ctx, allowed_roots=[case],
        )
        # 跨项目引用：B 项目 + A 的来源编号，该 (project, source) 组合未登记。
        self._expect_error(
            "planning_source_unregistered", conn,
            self._snapshot(project_id="proj-b", planning_source_id="src-a1",
                           path=path, meta=meta),
            context=ctx, allowed_roots=[case],
        )

        self.assertEqual(_changed_tables(before, self._state(conn)), [])

    # ------------------------------------------------------------------
    # 声明与登记不一致 / 注册允许根不被受控根覆盖
    # ------------------------------------------------------------------
    def test_registration_mismatch_and_uncovered_root_are_rejected(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        ctx = self._owner_ctx()
        before = self._state(conn)

        variants = (
            ("source_kind_mismatch",
             self._snapshot(path=path, meta=meta, source_kind="development-plan"),
             [case]),
            ("source_path_mismatch",
             self._snapshot(path=os.path.join(case, "other.md"), meta=meta),
             [case]),
            ("source_name_mismatch",
             self._snapshot(path=path, meta=meta, name="renamed.md"),
             [case]),
            ("registered_root_not_covered",
             self._snapshot(path=path, meta=meta),
             [os.path.join(case, "narrower")]),
        )
        for expected_reason, snapshot, roots in variants:
            with self.subTest(reason=expected_reason):
                error = self._expect_error(
                    "planning_source_mismatch", conn, snapshot,
                    context=ctx, allowed_roots=roots,
                )
                self.assertEqual(error.details.get("reason"), expected_reason)
        self.assertEqual(_changed_tables(before, self._state(conn)), [])

        # 已登记允许根落在受控根之外：拒绝，且不放宽到注册根。
        other_root = self._case_dir()
        conn2 = self._new_db()
        path2, meta2 = self._write_source(case)
        self._register_source(conn2, source_path=path2, allowed_root=other_root)
        before2 = self._state(conn2)
        error = self._expect_error(
            "planning_source_mismatch", conn2,
            self._snapshot(path=path2, meta=meta2),
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual(error.details.get("reason"), "registered_root_not_covered")
        self.assertEqual(_changed_tables(before2, self._state(conn2)), [])

    # ------------------------------------------------------------------
    # 受控元数据自报 / 未知字段 / 嵌入运行事实
    # ------------------------------------------------------------------
    def test_metadata_unknown_fields_and_execution_facts_are_rejected(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        ctx = self._owner_ctx()
        before = self._state(conn)
        base = self._snapshot(path=path, meta=meta)

        for field in CANDIDATE.CONTROLLED_METADATA_FIELDS:
            with self.subTest(metadata=field):
                snapshot = copy.deepcopy(base)
                snapshot[field] = "self-reported"
                error = self._expect_error(
                    "planning_snapshot_invalid", conn, snapshot,
                    context=ctx, allowed_roots=[case],
                )
                self.assertIn(
                    field, error.details.get("rejectedMetadataFields", [])
                )

        snapshot = copy.deepcopy(base)
        snapshot["snapshotOwnership"] = "worker"
        error = self._expect_error(
            "planning_snapshot_invalid", conn, snapshot,
            context=ctx, allowed_roots=[case],
        )
        self.assertIn("snapshotOwnership", error.details.get("unknownFields", []))

        # 嵌套运行事实 / 授权字段：沿用已通过摘要校验的原 code。
        snapshot = copy.deepcopy(base)
        snapshot["stages"] = [
            {"stageId": "stage-1", "title": "阶段一", "summary": "", "taskId": "task-1"}
        ]
        error = self._expect_error(
            "planning_execution_fact_forbidden", conn, snapshot,
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual(error.details["cause"]["code"], "planning_execution_fact_forbidden")

        snapshot = copy.deepcopy(base)
        snapshot["sections"] = [
            {"sectionId": "sec-1", "title": "目标", "anchor": "goal", "level": 2,
             "permissions": {"allowGit": False}}
        ]
        self._expect_error(
            "planning_execution_fact_forbidden", conn, snapshot,
            context=ctx, allowed_roots=[case],
        )

        # 嵌套摘要自引用：沿用已通过摘要校验的原 code。
        snapshot = copy.deepcopy(base)
        snapshot["businessLines"] = [
            {"businessLineId": "bl-1", "title": "线一", "planStatus": "draft",
             "canonicalDigest": "sha256:" + "0" * 64}
        ]
        self._expect_error(
            "planning_digest_self_reference", conn, snapshot,
            context=ctx, allowed_roots=[case],
        )

        for bad in ([], "snapshot", None, 42):
            with self.subTest(payload=type(bad).__name__):
                self._expect_error(
                    "planning_snapshot_invalid", conn, bad,
                    context=ctx, allowed_roots=[case],
                )

        snapshot = copy.deepcopy(base)
        snapshot.pop("projectId")
        self._expect_error(
            "planning_snapshot_invalid", conn, snapshot,
            context=ctx, allowed_roots=[case],
        )

        snapshot = copy.deepcopy(base)
        snapshot["projectId"] = "   "
        self._expect_error(
            "planning_snapshot_invalid", conn, snapshot,
            context=ctx, allowed_roots=[case],
        )

        # 缺少受控允许根（不能从 snapshot 取）：同样拒绝。
        snapshot = copy.deepcopy(base)
        with self.assertRaises(PlanningSnapshotImportError) as caught:
            import_planning_snapshot(conn, snapshot, context=ctx, allowed_roots=None)
        self.assertEqual(caught.exception.code, "planning_snapshot_invalid")

        # 基础拒绝阶段绝不读取来源文件。
        snapshot = copy.deepcopy(base)
        snapshot["canonicalDigest"] = "sha256:" + "0" * 64
        with mock.patch.object(
            CANDIDATE, "preflight_source", side_effect=AssertionError("基础拒绝不得读取来源")
        ):
            with self.assertRaises(PlanningSnapshotImportError) as caught:
                import_planning_snapshot(conn, snapshot, context=ctx, allowed_roots=[case])
        self.assertEqual(caught.exception.code, "planning_snapshot_invalid")

        self.assertEqual(_changed_tables(before, self._state(conn)), [])

    # ------------------------------------------------------------------
    # 授权失败：不读文件、不写库
    # ------------------------------------------------------------------
    def test_authorization_failures_never_read_files_or_write_database(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        before = self._state(conn)

        owner_with_capability = lambda: CallerContext(
            actor_id="cox", role="owner", capabilities=frozenset({IMPORT_CAPABILITY}),
            entry="local-review-cli",
        )
        callers = (
            ("none", None),
            ("dict", {"actorId": "cox", "role": "owner"}),
            ("worker", worker_context(synthetic=True)),
            ("source", source_context(synthetic=True)),
            ("reviewer", reviewer_context(synthetic=True)),
            ("owner_without_capability", owner_context(synthetic=True)),
            ("owner_entry_unspecified", CallerContext(
                actor_id="cox", role="owner",
                capabilities=frozenset({IMPORT_CAPABILITY}), entry="unspecified")),
            ("owner_entry_blank", CallerContext(
                actor_id="cox", role="owner",
                capabilities=frozenset({IMPORT_CAPABILITY}), entry="   ")),
            ("owner_actor_blank", CallerContext(
                actor_id="", role="owner",
                capabilities=frozenset({IMPORT_CAPABILITY}), entry="local-review-cli")),
        )
        for label, context in callers:
            with self.subTest(caller=label):
                with mock.patch.object(
                    CANDIDATE, "preflight_source",
                    side_effect=AssertionError("未授权调用不得读取来源"),
                ):
                    with self.assertRaises(PlanningSnapshotImportError) as caught:
                        import_planning_snapshot(
                            conn, snapshot, context=context, allowed_roots=[case]
                        )
                self.assertEqual(caught.exception.code, "permission_denied")
                self.assertFalse(conn.in_transaction)

        # 合法受控身份（owner + 能力 + 受控 entry）是唯一被放行的组合。
        result = import_planning_snapshot(
            conn, snapshot, context=owner_with_capability(), allowed_roots=[case]
        )
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(_changed_tables(before, self._state(conn)),
                         ["planning_audit", "planning_snapshots"])

    # ------------------------------------------------------------------
    # 存储未就绪：类型 / 调用方事务 / 外键 / schema 版本 / 缺表
    # ------------------------------------------------------------------
    def test_storage_not_ready_conditions_are_rejected(self):
        case = self._case_dir()
        path, meta = self._write_source(case)
        ctx = self._owner_ctx()
        snapshot = self._snapshot(path=path, meta=meta)

        def expect(conn, label):
            with self.subTest(storage=label):
                self._expect_error(
                    "planning_storage_not_ready", conn, snapshot,
                    context=ctx, allowed_roots=[case],
                )

        expect(object(), "not-a-connection")
        expect(self._connect(), "empty-database")
        expect(self._new_db(omit=("planning_snapshots",)), "missing-snapshot-table")
        expect(self._new_db(foreign_keys=False), "foreign-keys-off")
        expect(self._new_db(meta_version=2, meta_check=False), "schema-version-2")

        no_meta = self._new_db()
        no_meta.execute("DELETE FROM planning_schema_meta")
        no_meta.commit()
        expect(no_meta, "schema-meta-row-missing")

        # 调用方已有未提交事务：拒绝且不改动该事务。
        caller_txn = self._new_db()
        self._register_source(caller_txn, source_path=path, allowed_root=case)
        before = self._state(caller_txn)
        caller_txn.execute("BEGIN IMMEDIATE")
        self._expect_error(
            "planning_storage_not_ready", caller_txn, snapshot,
            context=ctx, allowed_roots=[case],
        )
        self.assertTrue(caller_txn.in_transaction)
        self.assertEqual(_changed_tables(before, self._state(caller_txn)), [])
        caller_txn.rollback()

    # ------------------------------------------------------------------
    # 来源非 read / 声明 SHA 与字节不符
    # ------------------------------------------------------------------
    def test_source_preflight_rejection_and_declared_metadata_drift(self):
        case = self._case_dir()
        ctx = self._owner_ctx()

        # 1) 已登记路径是目录：结论不是 read（unreadable）。
        conn_dir = self._new_db()
        dir_path = os.path.join(case, "not-a-file")
        os.makedirs(dir_path, exist_ok=True)
        self._register_source(conn_dir, source_path=dir_path, allowed_root=case)
        before = self._state(conn_dir)
        error = self._expect_error(
            "planning_source_preflight_rejected", conn_dir,
            self._snapshot(path=dir_path, meta={"bytes": 0, "sha256": "sha256:" + "0" * 64}),
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual(error.details.get("status"), "unreadable")
        self.assertEqual(_changed_tables(before, self._state(conn_dir)), [])

        # 2) 已登记路径是符号链接：明确 rejected。
        real_path, real_meta = self._write_source(case, name="real.md")
        link_path = os.path.join(case, "link.md")
        os.symlink(real_path, link_path)
        conn_link = self._new_db()
        self._register_source(conn_link, source_path=link_path, allowed_root=case)
        error = self._expect_error(
            "planning_source_preflight_rejected", conn_link,
            self._snapshot(path=link_path, meta=real_meta),
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual(error.details.get("status"), "rejected")

        # 3) 声明 sha256 与原文不符：drift。
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        before = self._state(conn)
        snapshot = self._snapshot(path=path, meta=meta)
        snapshot["sourceFile"]["sha256"] = "sha256:" + "a" * 64
        error = self._expect_error(
            "planning_source_drift", conn, snapshot, context=ctx, allowed_roots=[case]
        )
        self.assertEqual(error.details.get("reason"), "planning_source_mismatch")
        self.assertEqual(_changed_tables(before, self._state(conn)), [])

        # 4) 声明 bytes 与原文不符：drift。
        snapshot = self._snapshot(path=path, meta=meta)
        snapshot["sourceFile"]["bytes"] = meta["bytes"] + 1
        error = self._expect_error(
            "planning_source_drift", conn, snapshot, context=ctx, allowed_roots=[case]
        )
        self.assertEqual(error.details.get("reason"), "planning_source_mismatch")

        # 来源原文未被改写。
        self.assertEqual(_file_meta(path), meta)

    # ------------------------------------------------------------------
    # 两次读取之间的漂移：优先于既有冲突，且零写入
    # ------------------------------------------------------------------
    def test_drift_between_the_two_reads_wins_over_existing_conflict(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        # 先放一条内容不同的既有快照：若走到冲突判定，本用例会误报冲突。
        existing_id = self._insert_existing_snapshot(
            conn, canonical_digest="sha256:" + "b" * 64, document_version="v0"
        )
        snapshot = self._snapshot(path=path, meta=meta, document_version="v1")
        before = self._state(conn)

        real_preflight = CANDIDATE.preflight_source
        calls = []

        def flaky_preflight(item, roots, **kwargs):
            result = real_preflight(item, roots, **kwargs)
            calls.append({
                "path": item.get("path"),
                "roots": list(roots),
                "expected": dict(kwargs),
                "result": dict(result),
            })
            if len(calls) == 1:
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write(_MUTATED_SOURCE_TEXT)
            return result

        with mock.patch.object(CANDIDATE, "preflight_source", flaky_preflight):
            error = self._expect_error(
                "planning_source_drift", conn, snapshot,
                context=self._owner_ctx(), allowed_roots=[case],
            )

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["path"], path)
        self.assertEqual(calls[0]["roots"], [os.path.realpath(case)])
        self.assertEqual(calls[0]["expected"]["expected_sha256"], meta["sha256"])
        self.assertEqual(calls[0]["expected"]["expected_bytes"], meta["bytes"])
        self.assertEqual(calls[0]["result"]["status"], "read")
        # 第二次读取用第一次的**实际**字节 / SHA 作为期待值。
        self.assertEqual(calls[1]["expected"]["expected_sha256"], calls[0]["result"]["sha256"])
        self.assertEqual(calls[1]["expected"]["expected_bytes"], calls[0]["result"]["bytes"])
        self.assertEqual(calls[1]["result"]["status"], "drift")
        # 第二次读取的失败先于任何幂等 / 内容冲突判定。
        self.assertEqual(error.details.get("stage"), "first_read_readback")

        self.assertEqual(_changed_tables(before, self._state(conn)), [])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0], 1
        )
        self.assertEqual(
            conn.execute("SELECT snapshot_id FROM planning_snapshots").fetchone()[0],
            existing_id,
        )

    # ------------------------------------------------------------------
    # 内容冲突：额外 revision 明确拒绝 / 同版本字节变更明确拒绝
    # ------------------------------------------------------------------
    def test_additional_revision_and_same_version_conflicts_are_rejected(self):
        case = self._case_dir()
        path, meta = self._write_source(case)
        ctx = self._owner_ctx()

        variants = (
            ("additional_revision_not_in_this_slice", "v0", "v2"),
            ("same_document_version_different_bytes", "v1", "v1"),
        )
        for expected_reason, existing_version, incoming_version in variants:
            with self.subTest(reason=expected_reason):
                conn = self._new_db()
                self._register_source(conn, source_path=path, allowed_root=case)
                existing_id = self._insert_existing_snapshot(
                    conn,
                    canonical_digest="sha256:" + "c" * 64,
                    document_version=existing_version,
                )
                before = self._state(conn)
                snapshot = self._snapshot(
                    path=path, meta=meta, document_version=incoming_version
                )
                error = self._expect_error(
                    "planning_snapshot_conflict", conn, snapshot,
                    context=ctx, allowed_roots=[case],
                )
                self.assertEqual(error.details.get("reason"), expected_reason)
                self.assertEqual(error.details.get("existingSnapshotIds"), [existing_id])
                self.assertEqual(_changed_tables(before, self._state(conn)), [])

    # ------------------------------------------------------------------
    # 缺映射诊断保留；planStatus 只是资料标签，候选不激活
    # ------------------------------------------------------------------
    def test_missing_mapping_diagnostics_keep_candidate_status(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        stages = [{"stageId": "stage-1", "title": "阶段一", "summary": ""}]
        snapshot = self._snapshot(path=path, meta=meta, plan_status="active", stages=stages)
        before = self._state(conn)

        result = import_planning_snapshot(
            conn, snapshot, context=self._owner_ctx(), allowed_roots=[case]
        )

        self.assertEqual(result["status"], "candidate")
        self.assertIs(result["wouldActivate"], False)
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertEqual(result["diagnostics"][0]["code"], "planning_mapping_missing")
        self.assertEqual(result["diagnostics"][0]["path"], "$.stages[0].sectionRef")

        row = conn.execute("SELECT status, payload_json FROM planning_snapshots").fetchone()
        self.assertEqual(row[0], "candidate")
        payload = json.loads(row[1])
        self.assertEqual(payload["planStatus"], "active")   # 资料标签，不代表激活
        self.assertEqual(payload["stages"], stages)

        detail = json.loads(
            conn.execute("SELECT detail_json FROM planning_audit").fetchone()[0]
        )
        self.assertEqual(detail["diagnosticsCount"], 1)

        # 只新增候选快照 + 审计；没有任何激活指针或执行事实。
        self.assertEqual(
            _changed_tables(before, self._state(conn)),
            ["planning_audit", "planning_snapshots"],
        )

    # ------------------------------------------------------------------
    # 审计写入被拒：整个自有事务回滚，零快照残留
    # ------------------------------------------------------------------
    def test_audit_insert_denied_by_authorizer_rolls_back_everything(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        before = self._state(conn)

        def deny_audit_insert(action, arg1, arg2, dbname, trigger):
            name = arg1.decode("utf-8") if isinstance(arg1, bytes) else arg1
            if action == sqlite3.SQLITE_INSERT and name == "planning_audit":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(deny_audit_insert)
        try:
            error = self._expect_error(
                "planning_snapshot_import_failed", conn, snapshot,
                context=self._owner_ctx(), allowed_roots=[case],
            )
        finally:
            # 回滚解除：authorizer 一律放行。
            conn.set_authorizer(lambda *args: sqlite3.SQLITE_OK)

        self.assertEqual(error.details.get("reason"), "transaction_failed")
        self.assertFalse(conn.in_transaction)
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0], 0
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0], 0
        )
        self.assertEqual(_changed_tables(before, self._state(conn)), [])

    # ------------------------------------------------------------------
    # 关闭重开读回 + 幂等
    # ------------------------------------------------------------------
    def test_file_database_reopen_readback_and_idempotency(self):
        case = self._case_dir()
        db_path = os.path.join(case, "planning.sqlite")
        conn = self._new_db(path=db_path)
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        ctx = self._owner_ctx()

        first = import_planning_snapshot(conn, snapshot, context=ctx, allowed_roots=[case])
        rows_before = conn.execute("SELECT * FROM planning_snapshots").fetchall()
        conn.close()

        reopened = sqlite3.connect(db_path)
        self._connections.append(reopened)
        reopened.execute("PRAGMA foreign_keys = ON")
        rows = reopened.execute(
            "SELECT snapshot_id, snapshot_revision, document_version, source_sha256, "
            "source_bytes, canonical_digest, payload_json, imported_at, status "
            "FROM planning_snapshots"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], first["snapshotId"])
        self.assertEqual(rows[0][1], 1)
        self.assertEqual(rows[0][2], "v1")
        self.assertEqual(rows[0][3], meta["sha256"])
        self.assertEqual(rows[0][4], meta["bytes"])
        self.assertEqual(rows[0][8], "candidate")
        payload = json.loads(rows[0][6])
        self.assertEqual(payload["snapshotId"], first["snapshotId"])
        self.assertEqual(payload["canonicalDigest"], rows[0][5])
        self.assertEqual(compute_canonical_digest(payload), rows[0][5])
        self.assertEqual(
            reopened.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0], 1
        )

        second = import_planning_snapshot(
            reopened, snapshot, context=ctx, allowed_roots=[case]
        )
        self.assertIs(second["replayed"], True)
        self.assertEqual(second["snapshotId"], first["snapshotId"])
        self.assertEqual(
            reopened.execute("SELECT * FROM planning_snapshots").fetchall(), rows_before
        )
        self.assertEqual(
            reopened.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0], 1
        )

    # ------------------------------------------------------------------
    # 执行账本 / Registry / 来源记录 / 原文一律不动
    # ------------------------------------------------------------------
    def test_ledger_registry_and_source_rows_stay_untouched(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        before = self._state(conn)
        sources_before = conn.execute("SELECT * FROM planning_sources").fetchall()
        file_before = _file_meta(path)

        result = import_planning_snapshot(
            conn, snapshot, context=self._owner_ctx(), allowed_roots=[case]
        )

        self.assertEqual(result["status"], "candidate")
        self.assertIs(result["wouldActivate"], False)
        after = self._state(conn)
        self.assertEqual(
            _changed_tables(before, after), ["planning_audit", "planning_snapshots"]
        )
        for table, _ in _LEDGER_TABLES:
            self.assertEqual(after[table], before[table])
        self.assertEqual(
            conn.execute("SELECT * FROM planning_sources").fetchall(), sources_before
        )
        self.assertEqual(
            conn.execute("SELECT * FROM workspace_projects").fetchall(), [("proj-a",)]
        )
        # 候选只读来源，不改写原文。
        self.assertEqual(_file_meta(path), file_before)
        self.assertEqual(meta, file_before)


# ---------------------------------------------------------------------------
# 024 返工回归（仅新增，上面 16 个原方法保持不变）
#   1) ``_narrowest_common_root`` 必须按路径组件判断包含：根 ``/`` 覆盖合法子目录，
#      同前缀但非祖先的相邻目录不得被误判。
#   2) 真实冻结正式 schema（迁移 + 来源登记）下的根 ``/`` 端到端回归。
# ---------------------------------------------------------------------------


class RootContainmentPathComponentCase(unittest.TestCase):
    """直接校验 ``_narrowest_common_root`` 的路径组件包含判断（不读文件、不写库）。"""

    def test_root_slash_contains_registered_subdirectory(self):
        narrowest = CANDIDATE._narrowest_common_root
        registered = os.path.join(tempfile.gettempdir(), "d4-root-case", "sub")

        # 旧实现把 "/" 拼成 "//" 而误拒；返工后必须按组件覆盖。
        self.assertEqual(narrowest(registered, ["/"]), os.path.realpath(registered))
        self.assertEqual(narrowest("/", ["/"]), os.path.realpath("/"))
        # 命中顺序无关：前面的受控根不覆盖时继续看后面的 "/"。
        self.assertEqual(
            narrowest(registered, ["/d4-definitely-not-an-ancestor", "/"]),
            os.path.realpath(registered),
        )
        # 结果仍是更窄的注册根，绝不放宽成 "/"。
        self.assertNotEqual(narrowest(registered, ["/"]), os.path.realpath("/"))

    def test_adjacent_same_prefix_directory_is_not_contained(self):
        narrowest = CANDIDATE._narrowest_common_root
        base = os.path.join(tempfile.gettempdir(), "d4-root-prefix")
        registered = os.path.join(base, "alpha")
        sibling = os.path.join(base, "alpha-beta")

        # 同前缀但非祖先：不得误判为包含，更不得放宽到注册根。
        self.assertIsNone(narrowest(registered, [sibling]))
        # 真正的祖先目录仍覆盖。
        self.assertEqual(narrowest(registered, [base]), os.path.realpath(registered))
        self.assertEqual(narrowest(registered, ["/"]), os.path.realpath(registered))
        # 非法 / 不可比较的注册根一律拒绝。
        self.assertIsNone(narrowest("relative/root", ["/"]))
        self.assertIsNone(narrowest("   ", ["/"]))
        self.assertIsNone(narrowest(None, ["/"]))


class FormalSchemaRootContainmentEndToEndCase(unittest.TestCase):
    """真实冻结正式 schema 上的根 ``/`` 端到端回归（只读 formal，隔离临时库）。

    候选模块本身仍不 connect / 不迁移 / 不建表；本用例只在 ``tests/runtime`` 下的
    每用例独立临时库中调用正式迁移与来源登记入口，校验完即关闭连接并清理自有目录。
    """

    def setUp(self):
        os.makedirs(RUNTIME_ROOT, exist_ok=True)
        self._owned_dirs = []
        self._connections = []
        from service.planning_source_register import register_planning_source
        from storage.planning_schema import migrate_planning_schema

        self._register_planning_source = register_planning_source
        self._migrate_planning_schema = migrate_planning_schema

    def tearDown(self):
        for conn in self._connections:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        # 只删除本用例自己创建的目录，绝不 rmtree 已存在的未知目录。
        for path in self._owned_dirs:
            shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------
    # 资源
    # ------------------------------------------------------------------
    def _case_dir(self, prefix="formal-case-"):
        path = tempfile.mkdtemp(prefix=prefix, dir=RUNTIME_ROOT)
        self._owned_dirs.append(path)
        return path

    def _connect(self, path=":memory:"):
        conn = sqlite3.connect(path)
        conn.execute("PRAGMA foreign_keys = ON")
        self._connections.append(conn)
        return conn

    def _formal_db(self, conn, project_id="proj-a"):
        """登记项目 + 正式迁移：完全沿用冻结 formal 迁移，不复制 DDL。"""
        conn.execute("CREATE TABLE workspace_projects (project_id TEXT PRIMARY KEY)")
        conn.execute(
            "INSERT INTO workspace_projects (project_id) VALUES (?)", (project_id,)
        )
        conn.commit()
        self._migrate_planning_schema(conn)
        conn.commit()
        return conn

    def _caps_context(self):
        """同时具备登记与保存能力；entry 受控、保留 synthetic、仅本地合成。"""
        return CallerContext(
            actor_id="cox",
            role="owner",
            capabilities=frozenset({"planning.source.register", IMPORT_CAPABILITY}),
            entry="cox-independent-synthetic-probe",
            synthetic=True,
        )

    def _register(self, conn, path, allowed_root, project_id="proj-a"):
        return self._register_planning_source(
            conn,
            {"projectId": project_id, "sourceKind": "product", "path": path},
            self._caps_context(),
            [allowed_root],
        )

    def _write_source(self, directory, name="plan.md"):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# planning\n")
        return path

    def _payload(self, path, planning_source_id, name=None, project_id="proj-a"):
        with open(path, "rb") as handle:
            raw = handle.read()
        return {
            "schemaVersion": "planning-snapshot.v1",
            "projectId": project_id,
            "planningSourceId": planning_source_id,
            "sourceKind": "product",
            "sourceFile": {
                "name": os.path.basename(path) if name is None else name,
                "path": path,
                "documentVersion": "v1",
                "sha256": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "bytes": len(raw),
            },
            "sections": [],
            "stages": [],
            "businessLines": [],
            "milestones": [],
            "planStatus": "planned",
        }

    # ------------------------------------------------------------------
    # 根 "/"：正式 schema 下首次保存 + 幂等 + 不受控根覆盖（返工主回归）
    # ------------------------------------------------------------------
    def test_root_slash_covers_registered_root_on_formal_schema(self):
        case = self._case_dir()
        conn = self._connect()
        self._formal_db(conn)
        source = self._write_source(case)
        registration = self._register(conn, source, case)
        payload = self._payload(source, registration["planningSourceId"])
        ctx = self._caps_context()

        sources_before = conn.execute("SELECT * FROM planning_sources").fetchall()
        with open(source, "rb") as handle:
            file_before = handle.read()

        first = import_planning_snapshot(conn, payload, ctx, [case])
        self.assertEqual(first["status"], "candidate")
        self.assertEqual(first["snapshotRevision"], 1)
        self.assertIs(first["replayed"], False)
        self.assertIs(first["wouldActivate"], False)
        self.assertTrue(first["snapshotId"].startswith("snap-"))

        snapshots_before = conn.execute("SELECT * FROM planning_snapshots").fetchall()
        audits_before = conn.execute("SELECT * FROM planning_audit").fetchall()

        # 同一受控根的幂等重放：无新增。
        replay = import_planning_snapshot(conn, payload, ctx, [case])
        self.assertIs(replay["replayed"], True)
        self.assertEqual(replay["snapshotId"], first["snapshotId"])

        # 返工点：受控根为 "/" 时仍按路径组件覆盖已登记根（旧实现误拒）。
        root_replay = import_planning_snapshot(conn, payload, ctx, ["/"])
        self.assertIs(root_replay["replayed"], True)
        self.assertEqual(root_replay["snapshotId"], first["snapshotId"])
        self.assertEqual(root_replay["snapshotRevision"], 1)
        self.assertIs(root_replay["wouldActivate"], False)
        self.assertFalse(conn.in_transaction)

        # 根路径回归不改写来源行 / 原文，也不产生新快照与审计。
        self.assertEqual(
            conn.execute("SELECT * FROM planning_sources").fetchall(), sources_before
        )
        self.assertEqual(
            conn.execute("SELECT * FROM planning_snapshots").fetchall(), snapshots_before
        )
        self.assertEqual(
            conn.execute("SELECT * FROM planning_audit").fetchall(), audits_before
        )
        with open(source, "rb") as handle:
            self.assertEqual(handle.read(), file_before)

    # ------------------------------------------------------------------
    # 相邻同前缀目录不得被当成包含：明确拒绝且零写入
    # ------------------------------------------------------------------
    def test_adjacent_same_prefix_root_rejected_on_formal_schema(self):
        case = self._case_dir()
        registered_root = os.path.join(case, "alpha")
        sibling_root = os.path.join(case, "alpha-beta")
        os.makedirs(registered_root, exist_ok=True)
        os.makedirs(sibling_root, exist_ok=True)

        conn = self._connect()
        self._formal_db(conn)
        source = self._write_source(registered_root)
        registration = self._register(conn, source, registered_root)
        payload = self._payload(source, registration["planningSourceId"])
        ctx = self._caps_context()

        before = conn.execute("SELECT * FROM planning_snapshots").fetchall()
        # 登记本身会写一条审计：以登记后的基线做「零新增」比较，不假设绝对行数。
        audits_before = conn.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0]
        self.assertGreaterEqual(audits_before, 1)
        error = self._expect_error("planning_source_mismatch", conn, payload, ctx,
                                   [sibling_root])
        self.assertEqual(error.details.get("reason"), "registered_root_not_covered")
        self.assertEqual(
            conn.execute("SELECT * FROM planning_snapshots").fetchall(), before
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0],
            audits_before,
        )
        self.assertFalse(conn.in_transaction)

    # ------------------------------------------------------------------
    # 根 "/" 首次保存也必须是零执行事实、零激活：账本哨兵始终不动
    # ------------------------------------------------------------------
    def test_root_slash_first_save_keeps_candidate_and_ledger_untouched(self):
        case = self._case_dir()
        db_path = os.path.join(case, "planning.sqlite")
        conn = self._connect(db_path)
        self._formal_db(conn)
        for table, key in _LEDGER_TABLES:
            conn.execute(
                "CREATE TABLE %s (%s TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                % (table, key)
            )
            conn.execute(
                "INSERT INTO %s (%s, payload) VALUES (?, ?)" % (table, key),
                ("sentinel-1", '{"sentinel":true}'),
            )
        conn.commit()
        source = self._write_source(case)
        registration = self._register(conn, source, case)
        payload = self._payload(source, registration["planningSourceId"])
        ctx = self._caps_context()

        ledger_before = {table: _table_rows(conn, table) for table, _ in _LEDGER_TABLES}
        # 登记写入的审计行计入基线：本用例只断言「保存动作恰好新增 1 条」。
        audits_before = conn.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0]
        snapshots_before = conn.execute(
            "SELECT COUNT(*) FROM planning_snapshots"
        ).fetchone()[0]

        first = import_planning_snapshot(conn, payload, ctx, ["/"])
        self.assertEqual(first["status"], "candidate")
        self.assertIs(first["wouldActivate"], False)

        for table, _ in _LEDGER_TABLES:
            self.assertEqual(_table_rows(conn, table), ledger_before[table])
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0],
            snapshots_before + 1,
        )
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0],
            audits_before + 1,
        )
        conn.close()

        reopened = sqlite3.connect(db_path)
        self._connections.append(reopened)
        reopened.execute("PRAGMA foreign_keys = ON")
        row = reopened.execute(
            "SELECT snapshot_id, snapshot_revision, status FROM planning_snapshots"
        ).fetchone()
        self.assertEqual(row[0], first["snapshotId"])
        self.assertEqual(row[1], 1)
        self.assertEqual(row[2], "candidate")

    def _expect_error(self, code, conn, snapshot, context, allowed_roots):
        with self.assertRaises(PlanningSnapshotImportError) as caught:
            import_planning_snapshot(
                conn, snapshot, context=context, allowed_roots=allowed_roots
            )
        self.assertEqual(caught.exception.code, code)
        return caught.exception


if __name__ == "__main__":
    unittest.main()
