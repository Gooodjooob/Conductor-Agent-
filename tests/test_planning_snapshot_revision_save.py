# -*- coding: utf-8 -*-
"""受控规划 revision 追加保存接线的隔离单元测试。

状态：**NOT_RUN**。Worker 无 bash 权限，本轮未运行任何命令；全部结果由 cox 独立执行
以下入口后判定（在 workspace 根目录运行）：

    /usr/bin/python3 -m unittest discover -s tests -p test_planning_snapshot_revision_save.py -v

约定（与 ``inputs/cox-revision-save-decision.md`` 一致）：

* 只用标准库 ``unittest`` / ``mock``；不引入 pytest、不安装依赖、不使用 skip。
* 正式依赖路径**只来自** ``inputs/baseline-manifest.json`` 的 ``formalRoot``，不猜父目录、
  不读环境变量覆盖、不做 fallback（候选模块自身也只做直接导入）。
* 候选模块用 ``spec_from_file_location`` 按 workspace 路径加载；正式依赖从冻结 formal
  根只读导入；**不**以「formal 处不存在同名模块」作断言。
* 候选实现**不 connect / 不迁移 / 不建表**；本文件只在隔离临时库（内存或 ``tests/runtime``
  下每用例独立目录）里自建镜像 schema，产品自身绝不连接数据库。
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

WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST_PATH = os.path.join(WORKSPACE_ROOT, "inputs", "baseline-manifest.json")
CANDIDATE_PATH = os.path.join(
    WORKSPACE_ROOT, "service", "planning_snapshot_revision_save.py"
)
RUNTIME_ROOT = os.path.join(WORKSPACE_ROOT, "tests", "runtime")

with open(MANIFEST_PATH, "r", encoding="utf-8") as _manifest_handle:
    _MANIFEST = json.load(_manifest_handle)

#: 正式依赖根只由冻结 manifest 明确，不做任何猜测 / 环境覆盖 / fallback。
FORMAL_ROOT = _MANIFEST["formalRoot"]
if not os.path.isdir(FORMAL_ROOT):
    raise AssertionError("baseline-manifest.json 的 formalRoot 不存在：%r" % FORMAL_ROOT)
if FORMAL_ROOT not in sys.path:
    sys.path.insert(0, FORMAL_ROOT)

from contracts.planning_digest import compute_canonical_digest  # noqa: E402
from contracts.planning_revision import (  # noqa: E402
    PlanningRevisionError as FormalRevisionError,
    decide_planning_revision as formal_decide_planning_revision,
)
from contracts.planning_structure import (  # noqa: E402
    validate_planning_structure as formal_validate_structure,
)
from service import planning_snapshot_import as formal_first_import  # noqa: E402
from service.context import (  # noqa: E402
    CallerContext,
    owner_context,
    reviewer_context,
    source_context,
    worker_context,
)

_CANDIDATE_NAME = "planning_snapshot_revision_save_candidate"
_SPEC = importlib.util.spec_from_file_location(_CANDIDATE_NAME, CANDIDATE_PATH)
CANDIDATE = importlib.util.module_from_spec(_SPEC)
sys.modules[_CANDIDATE_NAME] = CANDIDATE
_SPEC.loader.exec_module(CANDIDATE)

save_planning_snapshot_revision = CANDIDATE.save_planning_snapshot_revision
PlanningSnapshotRevisionSaveError = CANDIDATE.PlanningSnapshotRevisionSaveError
IMPORT_CAPABILITY = formal_first_import.PLANNING_SNAPSHOT_IMPORT
CONTROLLED_METADATA_FIELDS = formal_first_import.CONTROLLED_METADATA_FIELDS

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
_APPENDED_SOURCE_TEXT = "# 产品规划\n\n## 目标\n\n- 目标一\n- 目标二（新增）\n"
_SEED_SHA = "sha256:" + "1" * 64
_SEED_DIGEST = "sha256:" + "b" * 64


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


class RevisionSaveCase(unittest.TestCase):
    def setUp(self):
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

    def _connect(self, path=":memory:", foreign_keys=True, timeout=None):
        conn = sqlite3.connect(path) if timeout is None else sqlite3.connect(
            path, timeout=timeout
        )
        if foreign_keys:
            conn.execute("PRAGMA foreign_keys = ON")
        self._connections.append(conn)
        return conn

    def _write_source(self, directory, name="planning.md", text=None):
        path = os.path.join(directory, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(_SOURCE_TEXT if text is None else text)
        return path, _file_meta(path)

    def _rewrite_source(self, path, text):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return _file_meta(path)

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

    def _insert_existing_snapshot(self, conn, *, canonical_digest,
                                  document_version, project_id="proj-a",
                                  planning_source_id="src-a1", revision=1,
                                  source_sha256=None, source_bytes=1):
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
                source_sha256 or _SEED_SHA,
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

    def _expect_error(self, code, conn, snapshot, *, context, allowed_roots,
                      new_content_reason=None):
        with self.assertRaises(PlanningSnapshotRevisionSaveError) as caught:
            save_planning_snapshot_revision(
                conn, snapshot, context=context, allowed_roots=allowed_roots,
                new_content_reason=new_content_reason,
            )
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    # ------------------------------------------------------------------
    # 首次候选：系统编号 / revision=1 / 候选 / 审计
    # ------------------------------------------------------------------
    def test_first_save_creates_candidate_revision_one(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        before = self._state(conn)

        result = save_planning_snapshot_revision(
            conn, snapshot, context=self._owner_ctx(), allowed_roots=[case]
        )

        self.assertEqual(result["projectId"], "proj-a")
        self.assertEqual(result["planningSourceId"], "src-a1")
        self.assertTrue(result["snapshotId"].startswith("snap-"))
        self.assertEqual(len(result["snapshotId"]), len("snap-") + 32)
        self.assertIs(result["replayed"], False)
        self.assertEqual(result["snapshotRevision"], 1)
        self.assertNotIsInstance(result["snapshotRevision"], bool)
        self.assertEqual(result["status"], "candidate")
        self.assertIs(result["wouldActivate"], False)
        self.assertRegex(result["canonicalDigest"], r"^sha256:[0-9a-f]{64}$")
        self.assertEqual(result["diagnostics"], [])
        self.assertFalse(conn.in_transaction)

        rows = conn.execute(
            "SELECT snapshot_id, snapshot_revision, document_version, source_sha256, "
            "source_bytes, canonical_digest, imported_by_json, imported_at, status "
            "FROM planning_snapshots"
        ).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], result["snapshotId"])
        self.assertEqual(rows[0][1], 1)
        self.assertEqual(rows[0][2], "v1")
        self.assertEqual(rows[0][3], meta["sha256"])
        self.assertEqual(rows[0][4], meta["bytes"])
        self.assertEqual(rows[0][8], "candidate")
        imported_at = datetime.fromisoformat(rows[0][7])
        self.assertIsNotNone(imported_at.tzinfo)
        self.assertEqual(imported_at.utcoffset(), timedelta(0))

        audits = conn.execute(
            "SELECT audit_id, action, actor_json, reason, occurred_at, detail_json "
            "FROM planning_audit"
        ).fetchall()
        self.assertEqual(len(audits), 1)
        self.assertTrue(audits[0][0].startswith("pa-"))
        self.assertEqual(audits[0][1], "snapshot.import")
        self.assertEqual(audits[0][2], rows[0][6])
        self.assertEqual(audits[0][3], "explicit controlled first snapshot import")
        self.assertEqual(audits[0][4], rows[0][7])
        detail = json.loads(audits[0][5])
        self.assertEqual(detail["snapshotRevision"], 1)
        self.assertEqual(detail["documentVersion"], "v1")

        # 只新增快照 + 审计；账本 / Registry / 来源 / 项目都不动。
        self.assertEqual(
            _changed_tables(before, self._state(conn)),
            ["planning_audit", "planning_snapshots"],
        )

    # ------------------------------------------------------------------
    # 重放：原身份 / 原时间 / 零新增
    # ------------------------------------------------------------------
    def test_replay_returns_original_identity_without_new_rows(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        ctx = self._owner_ctx()

        first = save_planning_snapshot_revision(
            conn, snapshot, context=ctx, allowed_roots=[case]
        )
        rows_before = conn.execute("SELECT * FROM planning_snapshots").fetchall()
        audits_before = conn.execute("SELECT * FROM planning_audit").fetchall()

        second = save_planning_snapshot_revision(
            conn, snapshot, context=ctx, allowed_roots=[case]
        )

        self.assertIs(second["replayed"], True)
        self.assertEqual(second["snapshotId"], first["snapshotId"])
        self.assertEqual(second["snapshotRevision"], 1)
        self.assertEqual(second["status"], "candidate")
        self.assertIs(second["wouldActivate"], False)
        self.assertEqual(second["canonicalDigest"], first["canonicalDigest"])
        self.assertEqual(second["importedAt"], first["importedAt"])
        self.assertEqual(second["importedBy"], first["importedBy"])
        self.assertEqual(
            conn.execute("SELECT * FROM planning_snapshots").fetchall(), rows_before
        )
        self.assertEqual(
            conn.execute("SELECT * FROM planning_audit").fetchall(), audits_before
        )

    # ------------------------------------------------------------------
    # 明确新版本：max+1；无 reason 时审计说明新 documentVersion
    # ------------------------------------------------------------------
    def test_explicit_new_version_appends_with_max_plus_one(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        self._insert_existing_snapshot(
            conn, canonical_digest=_SEED_DIGEST, document_version="v1"
        )
        snapshot = self._snapshot(path=path, meta=meta, document_version="v2")
        before = self._state(conn)

        result = save_planning_snapshot_revision(
            conn, snapshot, context=self._owner_ctx(), allowed_roots=[case]
        )

        self.assertIs(result["replayed"], False)
        self.assertEqual(result["snapshotRevision"], 2)
        self.assertEqual(result["status"], "candidate")
        self.assertIs(result["wouldActivate"], False)
        self.assertEqual(
            conn.execute(
                "SELECT reason FROM planning_audit"
            ).fetchone()[0],
            "new document version v2",
        )
        revisions = sorted(
            row[0]
            for row in conn.execute(
                "SELECT snapshot_revision FROM planning_snapshots"
            ).fetchall()
        )
        self.assertEqual(revisions, [1, 2])
        self.assertEqual(
            _changed_tables(before, self._state(conn)),
            ["planning_audit", "planning_snapshots"],
        )

    # ------------------------------------------------------------------
    # unverified + 受控 reason：追加并原样保存 reason
    # ------------------------------------------------------------------
    def test_unverified_with_provided_reason_appends_and_keeps_reason(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        self._insert_existing_snapshot(
            conn, canonical_digest=_SEED_DIGEST, document_version="v1"
        )
        snapshot = self._snapshot(path=path, meta=meta, document_version="unverified")

        result = save_planning_snapshot_revision(
            conn, snapshot, context=self._owner_ctx(), allowed_roots=[case],
            new_content_reason="controlled append: owner approved new draft",
        )

        self.assertIs(result["replayed"], False)
        self.assertEqual(result["snapshotRevision"], 2)
        self.assertEqual(
            conn.execute("SELECT reason FROM planning_audit").fetchone()[0],
            "controlled append: owner approved new draft",
        )

    # ------------------------------------------------------------------
    # 不连续既有 revision：append = max + 1（不是 len + 1）
    # ------------------------------------------------------------------
    def test_noncontiguous_revisions_use_max_plus_one(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        self._insert_existing_snapshot(
            conn, canonical_digest=_SEED_DIGEST, document_version="v1", revision=1
        )
        self._insert_existing_snapshot(
            conn, canonical_digest="sha256:" + "c" * 64, document_version="v3",
            revision=3,
        )
        snapshot = self._snapshot(path=path, meta=meta, document_version="v4")

        result = save_planning_snapshot_revision(
            conn, snapshot, context=self._owner_ctx(), allowed_roots=[case]
        )

        self.assertEqual(result["snapshotRevision"], 4)
        revisions = sorted(
            row[0]
            for row in conn.execute(
                "SELECT snapshot_revision FROM planning_snapshots"
            ).fetchall()
        )
        self.assertEqual(revisions, [1, 3, 4])

    # ------------------------------------------------------------------
    # 同 documentVersion 不同字节 / SHA：硬拒，reason 不能豁免
    # ------------------------------------------------------------------
    def test_same_document_version_different_bytes_or_sha_is_hard_conflict(self):
        case = self._case_dir()
        path, meta = self._write_source(case)
        ctx = self._owner_ctx()

        variants = (
            # 同名但 SHA 与真实文件不同（bytes 与真实一致）。
            ("different_sha", _SEED_SHA, meta["bytes"]),
            # 同名且 SHA 与真实一致，但 bytes 不同。
            ("different_bytes", meta["sha256"], meta["bytes"] + 1),
        )
        for label, seeded_sha, seeded_bytes in variants:
            for reason in (None, "owner insists this is a new draft"):
                with self.subTest(variant=label, reason=reason):
                    conn = self._new_db()
                    self._register_source(conn, source_path=path, allowed_root=case)
                    self._insert_existing_snapshot(
                        conn, canonical_digest=_SEED_DIGEST, document_version="v1",
                        source_sha256=seeded_sha, source_bytes=seeded_bytes,
                    )
                    before = self._state(conn)
                    snapshot = self._snapshot(path=path, meta=meta, document_version="v1")
                    error = self._expect_error(
                        "planning_snapshot_conflict", conn, snapshot,
                        context=ctx, allowed_roots=[case],
                        new_content_reason=reason,
                    )
                    self.assertEqual(
                        error.details.get("reason"),
                        "same_document_version_different_bytes",
                    )
                    self.assertEqual(_changed_tables(before, self._state(conn)), [])

    # ------------------------------------------------------------------
    # 无声明：unverified + 无 reason 一律拒绝
    # ------------------------------------------------------------------
    def test_unverified_without_declaration_is_rejected(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        self._insert_existing_snapshot(
            conn, canonical_digest=_SEED_DIGEST, document_version="v1"
        )
        before = self._state(conn)
        snapshot = self._snapshot(path=path, meta=meta, document_version="unverified")

        error = self._expect_error(
            "planning_snapshot_conflict", conn, snapshot,
            context=self._owner_ctx(), allowed_roots=[case],
        )

        self.assertEqual(
            error.details.get("reason"), "new_content_declaration_required"
        )
        self.assertEqual(_changed_tables(before, self._state(conn)), [])

    # ------------------------------------------------------------------
    # 两次对拍漂移优先于重放 / 冲突，且零残留
    # ------------------------------------------------------------------
    def _make_flaky_preflight(self, mutate_path, calls):
        """构造「读一次后改写目标文件」的预检桩，用于制造两次对拍之间的漂移。"""
        real_preflight = formal_first_import.preflight_source

        def flaky_preflight(item, roots, **kwargs):
            result = real_preflight(item, roots, **kwargs)
            calls.append(dict(result))
            with open(mutate_path, "w", encoding="utf-8") as handle:
                handle.write(_MUTATED_SOURCE_TEXT)
            return result

        return flaky_preflight

    def test_drift_between_two_reads_wins_over_replay_and_conflict(self):
        case = self._case_dir()
        ctx = self._owner_ctx()

        # 子例 A：既有行与本次内容同 digest（本应重放），漂移必须优先。
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        first = save_planning_snapshot_revision(conn, snapshot, context=ctx,
                                               allowed_roots=[case])
        rows_before = conn.execute("SELECT * FROM planning_snapshots").fetchall()
        audits_before = conn.execute("SELECT * FROM planning_audit").fetchall()

        calls_a = []
        with mock.patch.object(
            formal_first_import, "preflight_source",
            self._make_flaky_preflight(path, calls_a),
        ):
            error = self._expect_error(
                "planning_source_drift", conn, snapshot, context=ctx,
                allowed_roots=[case],
            )

        self.assertEqual(len(calls_a), 2)
        self.assertEqual(calls_a[0].get("status"), "read")
        self.assertEqual(calls_a[1].get("status"), "drift")
        self.assertEqual(error.details.get("stage"), "first_read_readback")
        self.assertEqual(
            conn.execute("SELECT * FROM planning_snapshots").fetchall(), rows_before
        )
        self.assertEqual(
            conn.execute("SELECT * FROM planning_audit").fetchall(), audits_before
        )
        self.assertEqual(
            conn.execute("SELECT snapshot_id FROM planning_snapshots").fetchone()[0],
            first["snapshotId"],
        )

        # 子例 B：既有行同 documentVersion 不同字节（本应冲突），漂移同样优先。
        case2 = self._case_dir()
        conn2 = self._new_db()
        path2, meta2 = self._write_source(case2)
        self._register_source(conn2, source_path=path2, allowed_root=case2)
        self._insert_existing_snapshot(
            conn2, canonical_digest=_SEED_DIGEST, document_version="v1",
            source_sha256=_SEED_SHA, source_bytes=meta2["bytes"] + 5,
        )
        before2 = self._state(conn2)
        snapshot2 = self._snapshot(path=path2, meta=meta2, document_version="v1")

        calls_b = []
        with mock.patch.object(
            formal_first_import, "preflight_source",
            self._make_flaky_preflight(path2, calls_b),
        ):
            error2 = self._expect_error(
                "planning_source_drift", conn2, snapshot2, context=ctx,
                allowed_roots=[case2],
            )
        self.assertEqual(error2.code, "planning_source_drift")
        self.assertEqual(error2.details.get("reason"), "planning_source_mismatch")
        self.assertEqual(_changed_tables(before2, self._state(conn2)), [])

    # ------------------------------------------------------------------
    # 审计写入被拒：整个自有事务回滚，零快照残留
    # ------------------------------------------------------------------
    def test_audit_insert_failure_rolls_back_whole_transaction(self):
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
    # 追加不改旧行、不改原文
    # ------------------------------------------------------------------
    def test_append_keeps_old_rows_and_source_bytes_untouched(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        ctx = self._owner_ctx()

        first = save_planning_snapshot_revision(
            conn, self._snapshot(path=path, meta=meta, document_version="v1"),
            context=ctx, allowed_roots=[case],
        )
        old_rows = conn.execute("SELECT * FROM planning_snapshots").fetchall()

        new_meta = self._rewrite_source(path, _APPENDED_SOURCE_TEXT)
        second = save_planning_snapshot_revision(
            conn, self._snapshot(path=path, meta=new_meta, document_version="v2"),
            context=ctx, allowed_roots=[case],
        )

        self.assertNotEqual(second["snapshotId"], first["snapshotId"])
        self.assertEqual(second["snapshotRevision"], 2)
        # 旧行（含首版）逐字节保持。
        rows = conn.execute("SELECT * FROM planning_snapshots").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertIn(old_rows[0], rows)
        self.assertEqual(_file_meta(path), new_meta)

    # ------------------------------------------------------------------
    # 多项目 / 多来源：各自独立序列，互不串
    # ------------------------------------------------------------------
    def test_projects_and_sources_keep_independent_sequences(self):
        case = self._case_dir()
        conn = self._new_db(projects=("proj-a", "proj-b"))
        path_a1, meta_a1 = self._write_source(case, name="a1.md")
        path_a2, meta_a2 = self._write_source(case, name="a2.md")
        path_b1, meta_b1 = self._write_source(case, name="b1.md")
        self._register_source(conn, project_id="proj-a", planning_source_id="src-a1",
                              source_path=path_a1, allowed_root=case)
        self._register_source(conn, project_id="proj-a", planning_source_id="src-a2",
                              source_path=path_a2, allowed_root=case)
        self._register_source(conn, project_id="proj-b", planning_source_id="src-b1",
                              source_path=path_b1, allowed_root=case)
        ctx = self._owner_ctx()

        a1 = save_planning_snapshot_revision(
            conn, self._snapshot(project_id="proj-a", planning_source_id="src-a1",
                                 path=path_a1, meta=meta_a1),
            context=ctx, allowed_roots=[case],
        )
        a2 = save_planning_snapshot_revision(
            conn, self._snapshot(project_id="proj-a", planning_source_id="src-a2",
                                 path=path_a2, meta=meta_a2),
            context=ctx, allowed_roots=[case],
        )
        b1 = save_planning_snapshot_revision(
            conn, self._snapshot(project_id="proj-b", planning_source_id="src-b1",
                                 path=path_b1, meta=meta_b1),
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual((a1["snapshotRevision"], a2["snapshotRevision"],
                          b1["snapshotRevision"]), (1, 1, 1))

        # 在 a1 上追加新版本，只有 a1 序列前进。
        new_meta = self._rewrite_source(path_a1, _APPENDED_SOURCE_TEXT)
        appended = save_planning_snapshot_revision(
            conn, self._snapshot(project_id="proj-a", planning_source_id="src-a1",
                                 path=path_a1, meta=new_meta, document_version="v2"),
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual(appended["snapshotRevision"], 2)

        per_source = dict(
            conn.execute(
                "SELECT project_id || '|' || planning_source_id, COUNT(*) "
                "FROM planning_snapshots GROUP BY project_id, planning_source_id"
            ).fetchall()
        )
        self.assertEqual(per_source["proj-a|src-a1"], 2)
        self.assertEqual(per_source["proj-a|src-a2"], 1)
        self.assertEqual(per_source["proj-b|src-b1"], 1)

        # a2 仍可独立重放，身份正确。
        replay_a2 = save_planning_snapshot_revision(
            conn, self._snapshot(project_id="proj-a", planning_source_id="src-a2",
                                 path=path_a2, meta=meta_a2),
            context=ctx, allowed_roots=[case],
        )
        self.assertIs(replay_a2["replayed"], True)
        self.assertEqual(replay_a2["snapshotId"], a2["snapshotId"])

    # ------------------------------------------------------------------
    # 授权失败：不读文件、不写库
    # ------------------------------------------------------------------
    def test_authorization_rejections_never_read_files_or_write(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        before = self._state(conn)

        legal = lambda: CallerContext(
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
                    formal_first_import, "preflight_source",
                    side_effect=AssertionError("未授权调用不得读取来源"),
                ):
                    with self.assertRaises(PlanningSnapshotRevisionSaveError) as caught:
                        save_planning_snapshot_revision(
                            conn, snapshot, context=context, allowed_roots=[case]
                        )
                self.assertEqual(caught.exception.code, "permission_denied")
                self.assertFalse(conn.in_transaction)

        result = save_planning_snapshot_revision(
            conn, snapshot, context=legal(), allowed_roots=[case]
        )
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(
            _changed_tables(before, self._state(conn)),
            ["planning_audit", "planning_snapshots"],
        )

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
    # 自报元数据 / 未知字段 / payload 内自报 reason / 嵌套运行事实
    # ------------------------------------------------------------------
    def test_self_reported_metadata_facts_and_reason_in_payload_are_rejected(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        ctx = self._owner_ctx()
        before = self._state(conn)
        base = self._snapshot(path=path, meta=meta)

        for field in CONTROLLED_METADATA_FIELDS:
            with self.subTest(metadata=field):
                snapshot = copy.deepcopy(base)
                snapshot[field] = "self-reported"
                error = self._expect_error(
                    "planning_snapshot_invalid", conn, snapshot,
                    context=ctx, allowed_roots=[case],
                )
                self.assertIn(field, error.details.get("rejectedMetadataFields", []))

        for field in ("snapshotOwnership", "reason", "newContentReason",
                      "revision", "digest"):
            with self.subTest(unknown=field):
                snapshot = copy.deepcopy(base)
                snapshot[field] = "self-reported"
                error = self._expect_error(
                    "planning_snapshot_invalid", conn, snapshot,
                    context=ctx, allowed_roots=[case],
                )
                self.assertIn(field, error.details.get("unknownFields", []))

        # 嵌套运行事实：沿用已通过摘要校验的原 code。
        snapshot = copy.deepcopy(base)
        snapshot["stages"] = [
            {"stageId": "stage-1", "title": "阶段一", "summary": "", "taskId": "task-1"}
        ]
        error = self._expect_error(
            "planning_execution_fact_forbidden", conn, snapshot,
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual(error.details["cause"]["code"], "planning_execution_fact_forbidden")

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

        # 缺少受控允许根（不能从 snapshot 取）。
        with self.assertRaises(PlanningSnapshotRevisionSaveError) as caught:
            save_planning_snapshot_revision(
                conn, base, context=ctx, allowed_roots=None
            )
        self.assertEqual(caught.exception.code, "planning_snapshot_invalid")

        self.assertEqual(_changed_tables(before, self._state(conn)), [])

    # ------------------------------------------------------------------
    # new_content_reason 严格类型
    # ------------------------------------------------------------------
    def test_new_content_reason_strict_type_is_enforced(self):
        case = self._case_dir()
        conn = self._new_db()
        path, meta = self._write_source(case)
        self._register_source(conn, source_path=path, allowed_root=case)
        snapshot = self._snapshot(path=path, meta=meta)
        ctx = self._owner_ctx()
        before = self._state(conn)

        for bad in (123, "", "   ", [], {}, True, 3.5):
            with self.subTest(reason=repr(bad)):
                error = self._expect_error(
                    "planning_revision_invalid", conn, snapshot,
                    context=ctx, allowed_roots=[case], new_content_reason=bad,
                )
                self.assertEqual(
                    error.details.get("reason"), "new_content_reason_invalid"
                )
        self.assertEqual(_changed_tables(before, self._state(conn)), [])

    # ------------------------------------------------------------------
    # 旧数据异常：全历史重复 digest / digest 与元数据矛盾
    # ------------------------------------------------------------------
    def test_duplicate_history_digest_and_digest_metadata_mismatch_are_rejected(self):
        case = self._case_dir()
        path, meta = self._write_source(case)
        ctx = self._owner_ctx()

        # 子例 A：全历史重复 canonicalDigest -> planning_revision_invalid。
        conn = self._new_db()
        self._register_source(conn, source_path=path, allowed_root=case)
        self._insert_existing_snapshot(
            conn, canonical_digest=_SEED_DIGEST, document_version="v1", revision=1
        )
        self._insert_existing_snapshot(
            conn, canonical_digest=_SEED_DIGEST, document_version="v2", revision=2
        )
        before = self._state(conn)
        snapshot = self._snapshot(path=path, meta=meta, document_version="v3")
        error = self._expect_error(
            "planning_revision_invalid", conn, snapshot,
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual(_changed_tables(before, self._state(conn)), [])

        # 子例 B：incoming digest 命中既有行但版本不同 -> digest_metadata_mismatch。
        conn2 = self._new_db()
        self._register_source(conn2, source_path=path, allowed_root=case)
        snapshot2 = self._snapshot(path=path, meta=meta, document_version="v1")
        incoming_digest = compute_canonical_digest(snapshot2)
        self._insert_existing_snapshot(
            conn2, canonical_digest=incoming_digest, document_version="v0",
            revision=1, source_sha256=meta["sha256"], source_bytes=meta["bytes"],
        )
        before2 = self._state(conn2)
        error2 = self._expect_error(
            "planning_snapshot_conflict", conn2, snapshot2,
            context=ctx, allowed_roots=[case],
        )
        self.assertEqual(error2.details.get("reason"), "digest_metadata_mismatch")
        self.assertEqual(_changed_tables(before2, self._state(conn2)), [])

    # ------------------------------------------------------------------
    # BEGIN IMMEDIATE 并发 busy：显式失败、不自动重试、零写入
    # ------------------------------------------------------------------
    def test_begin_immediate_busy_fails_explicitly_without_retry(self):
        case = self._case_dir()
        db_path = os.path.join(case, "planning.sqlite")
        setup = self._new_db(path=db_path)
        path, meta = self._write_source(case)
        self._register_source(setup, source_path=path, allowed_root=case)
        setup.commit()

        holder = self._connect(db_path)
        holder.execute("BEGIN IMMEDIATE")
        self.assertTrue(holder.in_transaction)

        waiter = self._connect(db_path, timeout=0.1)
        snapshot = self._snapshot(path=path, meta=meta)
        before = self._state(setup)

        with self.assertRaises(PlanningSnapshotRevisionSaveError) as caught:
            save_planning_snapshot_revision(
                waiter, snapshot, context=self._owner_ctx(), allowed_roots=[case]
            )
        error = caught.exception
        self.assertEqual(error.code, "planning_snapshot_import_failed")
        self.assertEqual(error.details.get("reason"), "begin_failed")
        self.assertFalse(waiter.in_transaction)

        holder.rollback()
        self.assertEqual(_changed_tables(before, self._state(setup)), [])

    # ------------------------------------------------------------------
    # 只读复用冻结 formal 依赖：不扫描父目录、不环境覆盖、不备用实现
    # ------------------------------------------------------------------
    def test_candidate_reuses_frozen_formal_dependencies_only(self):
        self.assertEqual(
            os.path.realpath(CANDIDATE.__file__), os.path.realpath(CANDIDATE_PATH)
        )
        self.assertIs(CANDIDATE.decide_planning_revision, formal_decide_planning_revision)
        self.assertIs(CANDIDATE.PlanningRevisionError, FormalRevisionError)
        self.assertIs(CANDIDATE.validate_planning_structure, formal_validate_structure)
        self.assertIs(CANDIDATE._first_import, formal_first_import)
        # 统一 wrapper 是新类型，不等于首版服务的错误类。
        self.assertIsNot(
            PlanningSnapshotRevisionSaveError,
            formal_first_import.PlanningSnapshotImportError,
        )

        # 候选路径在 sys.modules 中只绑定这一个模块对象。
        candidate_real = os.path.realpath(CANDIDATE_PATH)
        self.assertEqual(CANDIDATE.__spec__.name, _CANDIDATE_NAME)
        self.assertEqual(os.path.realpath(CANDIDATE.__spec__.origin), candidate_real)
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
        for module_name in (
            "contracts.planning_revision",
            "contracts.planning_structure",
            "contracts.planning_digest",
            "service.planning_snapshot_import",
            "service.context",
        ):
            with self.subTest(formal_dependency=module_name):
                module = sys.modules.get(module_name)
                self.assertIsNotNone(module)
                origin = os.path.realpath(module.__spec__.origin)
                self.assertEqual(
                    os.path.relpath(origin, formal_root_real),
                    os.path.join(*module_name.split(".")) + ".py",
                )

        # 候选源码不得自行 connect / 迁移 / executescript / 改写 / 调首版写入口。
        source = inspect.getsource(CANDIDATE)
        self.assertNotIn("sqlite3.connect", source)
        self.assertNotIn("migrate_planning_schema", source)
        self.assertNotIn("executescript(", source)
        self.assertNotIn("import_planning_snapshot", source)
        self.assertNotIn("D4_CONTRACTS_ROOT", source)
        self.assertNotIn("DELETE FROM", source)
        self.assertNotIn("UPDATE planning", source)


if __name__ == "__main__":
    unittest.main()
