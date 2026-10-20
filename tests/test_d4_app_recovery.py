"""candidate/scripts/d4_app_recovery.py 的离线 unittest 套件（G2-E 备份 / 恢复候选）。

NOT_RUN in this attempt
-----------------------
本 worker ``Bash=[]``：本轮**没有执行任何命令、没有运行任何用例、没有重算任何 SHA**。下面每个
用例都是 **NOT_RUN**，不是 PASS；真实运行、范围与冻结字节的独立核验属于 cox。

覆盖范围
--------
* 备份布局、清单字段、清单摘要与磁盘一致、0700/0600 权限；
* 备份后只读回读（存储层只读 API + 新连接）与来源 fixture 完全一致；
* 备份不改动任何来源文件、不产生 ``-wal``/``-journal`` 边车、只新建本次输出；
* 只读校验：缺失 / 被改 / 多余 / 符号链接文件与畸形 / 不完整清单；
* 数据库契约：完整 18 表 + ``integrity_check``；catalog 引用材料拷贝 sha256 一致性；
* 负面用例：来源缺失 / 未就绪 / WAL 头 / 输出已存在 / 非法输出名 / 非法 artifact_root /
  board SHA 不符 / 来源漂移（同连接 ``data_version`` 与 ``(device, inode)``）；
* 克隆：新建（不覆盖）、克隆后只读校验、恢复库 ``integrity_check`` 与“含义等价”，且不假设
  物理字节相同；来源快照在克隆前后未被改动；
* 用**已接受**的冻结读取器对新克隆路径 freeze + collect，确认恢复来源就绪（``ready=true``）；
* 无进程 / 无网络 / 无命令行入口 / 固定公共 API 与固定错误码。

fixture 与安全
--------------
每个 fixture 位于 ``candidate/tests/test-artifacts`` 下的一个 ``tempfile.mkdtemp`` 目录内，用例
结束即清理；冻结 G1 校验拒绝 ``/tmp`` 等临时路径，所以 fixture 从不放在那里。清单里冻结的
``candidate/tests/fixtures/d4_readiness_schema.sql`` 只被只读执行，绝不修改。所有合成数据的主键
都带 ``fixture-`` 前缀，**不是**任何真实业务数据；本套件不打开任何真实配置 / 来源 / 服务。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
_STORAGE_DIR = _TESTS_DIR.parent / "storage"
_TEST_ARTIFACTS = _TESTS_DIR / "test-artifacts"
_SCHEMA_FIXTURE = _TESTS_DIR / "fixtures" / "d4_readiness_schema.sql"


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, Path(path))
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load candidate module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rec = _load("d4_app_recovery", _SCRIPTS_DIR / "d4_app_recovery.py")
store_mod = _load("d4_sqlite_store_for_recovery_tests", _STORAGE_DIR / "sqlite_store.py")

PYTHON_PATH = "/usr/bin/python3"
PYTHON_AVAILABLE = os.path.isfile(PYTHON_PATH) and os.access(PYTHON_PATH, os.X_OK)
requires_python = unittest.skipUnless(
    PYTHON_AVAILABLE, "%s is not an executable file on this host" % PYTHON_PATH
)
requires_symlink = unittest.skipUnless(hasattr(os, "symlink"), "os.symlink is not available")

BOARD_SOURCE = '"""D4 board entry test double (fixture only)."""\nVALUE = 1\n'
OUTPUT_NAME = "D4 recovery"
CLONE_NAME = "D4 clone"
TASK_ID = "fixture-task-1"
SOURCE_ID = "fixture-source-1"
AGENT_ID = "fixture-agent-1"
PROJECT_ID = "fixture-project-live"
MATERIAL_SNAPSHOT = "materials/doc.md"
MATERIAL_BYTES = b"fixture material content\n"


def _fingerprint(paths):
    """对显式给出的路径列表做快照（不做目录枚举）。"""
    snapshot = {}
    for raw in paths:
        path = str(raw)
        try:
            info = os.lstat(path)
        except OSError:
            snapshot[path] = ("absent", None, None)
            continue
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            snapshot[path] = ("file", hashlib.sha256(Path(path).read_bytes()).hexdigest(), mode)
        elif stat.S_ISDIR(info.st_mode):
            snapshot[path] = ("dir", None, mode)
        else:
            snapshot[path] = ("other", None, mode)
    return snapshot


def catalog_document(*, project_id="d4-product", name="D4 看板", task_id="task-catalog-1",
                     status="verified", documents=None):
    return {
        "schemaVersion": "project-catalog.v1",
        "projects": [
            {
                "projectId": project_id,
                "name": name,
                "milestones": [{"id": "m-1", "name": "M1"}],
                "tasks": [
                    {
                        "taskId": task_id,
                        "milestoneId": "m-1",
                        "status": status,
                        "documents": [] if documents is None else documents,
                    }
                ],
                "issues": [],
            }
        ],
    }


def material_document(snapshot, content, *, name="doc.md", kind="markdown", source_path="archive/doc.md"):
    return {
        "name": name,
        "kind": kind,
        "snapshot": snapshot,
        "sha256": hashlib.sha256(content).hexdigest(),
        "sourcePath": source_path,
    }


class RecoveryFixtureTestCase(unittest.TestCase):
    """每个用例建立一个自清理的受控 root（位于 tests/test-artifacts 下）。"""

    def setUp(self):
        _TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self._sandbox = Path(
            tempfile.mkdtemp(prefix="d4-app-recovery-", dir=str(_TEST_ARTIFACTS))
        )
        self.addCleanup(shutil.rmtree, self._sandbox, ignore_errors=True)

        # 路径里刻意带上空格与单引号，检验路径处理。
        self.root = self._sandbox / "工作 目录'space"
        self.root.mkdir()
        self.app_dir = self.root / "app scripts"
        self.app_dir.mkdir()
        self.board_script = self.app_dir / "d4_board.py"
        self.board_script.write_text(BOARD_SOURCE, encoding="utf-8")
        self.board_sha = hashlib.sha256(BOARD_SOURCE.encode("utf-8")).hexdigest()

        self.db_path = self.root / "board 数据.sqlite3"
        self.catalog_path = self.root / "catalog json.json"
        self.material_path = self.root / MATERIAL_SNAPSHOT
        self.runtime_dir = self.root / "runtime dir"
        self.runtime_dir.mkdir()
        (self.runtime_dir / "logs").mkdir()
        self.artifact_root = self.root / "artifact root"
        self.artifact_root.mkdir()
        self.prepare_ready()

    # -- fixture writers -----------------------------------------------------
    def write_database(self, *, schema=True, seed=True):
        if self.db_path.exists() or self.db_path.is_symlink():
            self.db_path.unlink()
        connection = sqlite3.connect(str(self.db_path))
        try:
            if schema:
                connection.executescript(_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
            else:
                connection.execute("CREATE TABLE placeholder (value TEXT)")
            if schema and seed:
                self._seed(connection)
            connection.commit()
        finally:
            connection.close()

    def _seed(self, connection):
        """写入合成 fixture 行（task/agent/attempt/delivery/review/activity/workspace）。

        所有 id 都带 ``fixture-`` 前缀，明确是合成数据。本套件核对的是“读回一致”，不需要
        任何真实业务事实；当前 D4 存储契约没有 notif 表，因此不 seed notif。
        """
        connection.execute(
            "INSERT INTO sources (source_id, label, capability_json, registered_at) VALUES (?,?,?,?)",
            (SOURCE_ID, "fixture source", "{}", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO agents (agent_id, label, note, registered_at) VALUES (?,?,?,?)",
            (AGENT_ID, "fixture agent", None, "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO workspace_projects (project_id, name, goal, status, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            (PROJECT_ID, "fixture project", "fixture goal", "planned",
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO tasks (task_id, project_id, title, agent_id, source_id, status, revision, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (TASK_ID, PROJECT_ID, "fixture task", AGENT_ID, SOURCE_ID, "accepted", 1,
             "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO attempts (attempt_id, task_id, ordinal, state, created_at, submit_state) "
            "VALUES (?,?,?,?,?,?)",
            ("fixture-attempt-1", TASK_ID, 1, "running", "2026-01-01T00:00:00Z", "created"),
        )
        connection.execute(
            "INSERT INTO deliveries (delivery_id, task_id, attempt_id, summary, created_at) "
            "VALUES (?,?,?,?,?)",
            ("fixture-delivery-1", TASK_ID, "fixture-attempt-1", "fixture delivery",
             "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO reviews (review_id, task_id, delivery_id, decision, review_round, created_at) "
            "VALUES (?,?,?,?,?,?)",
            ("fixture-review-1", TASK_ID, "fixture-delivery-1", "approved", 1,
             "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO activity (task_id, attempt_id, at, kind, detail_json) VALUES (?,?,?,?,?)",
            (TASK_ID, "fixture-attempt-1", "2026-01-01T00:00:00Z", "started", "{}"),
        )
        connection.execute(
            "INSERT INTO workspace_workstreams (workstream_id, project_id, name, goal, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("fixture-workstream-1", PROJECT_ID, "fixture workstream", "goal", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO workspace_milestones (milestone_id, project_id, name, updated_at) "
            "VALUES (?,?,?,?)",
            ("fixture-milestone-1", PROJECT_ID, "fixture milestone", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO workspace_workers (worker_id, display_name, role, host, updated_at) "
            "VALUES (?,?,?,?,?)",
            ("fixture-worker-1", "fixture worker", "fixture-role", "localhost", "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO workspace_task_links (task_id, project_id, updated_at) VALUES (?,?,?)",
            (TASK_ID, PROJECT_ID, "2026-01-01T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO workspace_plan_sources (source_id, project_id, title, path, source_sha256, "
            "imported_at) VALUES (?,?,?,?,?,?)",
            ("fixture-plan-1", PROJECT_ID, "fixture plan", "plan.md", self._plan_digest(),
             "2026-01-01T00:00:00Z"),
        )

    @staticmethod
    def _plan_digest():
        return hashlib.sha256(b"fixture plan").hexdigest()

    def write_catalog(self, *, document=None, material=True):
        if material:
            self.material_path.parent.mkdir(parents=True, exist_ok=True)
            self.material_path.write_bytes(MATERIAL_BYTES)
        if document is None:
            document = catalog_document(documents=[material_document(MATERIAL_SNAPSHOT, MATERIAL_BYTES)])
        self.catalog_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

    def config(self, **overrides):
        config = {
            "schemaVersion": "d4-board-app-config.v1",
            "host": "127.0.0.1",
            "port": 8787,
            "dbPath": str(self.db_path),
            "catalogPath": str(self.catalog_path),
            "pythonPath": PYTHON_PATH,
            "boardScript": str(self.board_script),
            "runtimeDir": str(self.runtime_dir),
            "launchAgentLabel": "com.conductor.d4.board",
        }
        config.update(overrides)
        return config

    def prepare_ready(self):
        """有效、无项目冲突的 fixture：库内 project id 与 catalog 的 id 不同。"""
        self.write_database()
        self.write_catalog()
        return self.config()

    # -- helpers -------------------------------------------------------------
    def backup(self, *, config=None, output_name=OUTPUT_NAME, artifact_root=None):
        root = self.artifact_root if artifact_root is None else artifact_root
        return rec.backup_snapshot(
            self.config() if config is None else config,
            allowed_root=str(self.root),
            expected_board_sha256=self.board_sha,
            artifact_root=str(root),
            output_name=output_name,
        )

    def clone(self, snapshot_root, *, output_name=CLONE_NAME, artifact_root=None):
        root = self.artifact_root if artifact_root is None else artifact_root
        return rec.clone_snapshot(
            str(snapshot_root), artifact_root=str(root), output_name=output_name
        )

    def _read_rows_via_store(self, db_path):
        """用只读存储 API 在一个**新连接**上读回一组关键表（证明含义等价）。"""
        store = store_mod.SqliteStore(str(db_path), read_only=True)
        try:
            return {
                "source": store.get_source(SOURCE_ID),
                "agent": store.get_agent(AGENT_ID),
                "tasks": store.list_tasks(),
                "task": store.get_task(TASK_ID),
                "attempts": store.list_attempts(TASK_ID),
                "deliveries": store.list_deliveries(TASK_ID),
                "reviews": store.list_reviews(TASK_ID),
                "activity": store.list_activity(TASK_ID),
            }
        finally:
            store.close()

    def _watched_sources(self):
        return [
            self.db_path,
            self.catalog_path,
            self.material_path,
            self.board_script,
            Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-shm"),
            Path(str(self.db_path) + "-journal"),
        ]


# --------------------------------------------------------------------------- #
# 备份：布局 / 清单 / 权限 / 只读回读
# --------------------------------------------------------------------------- #
@requires_python
class BackupTests(RecoveryFixtureTestCase):
    def test_layout_and_manifest(self):
        manifest = self.backup()
        out = Path(manifest["outputRoot"])
        self.assertEqual(out, self.artifact_root / OUTPUT_NAME)
        self.assertEqual(manifest["schemaVersion"], "d4-app-recovery-manifest.v1")
        self.assertEqual(manifest["operation"], "backup")
        self.assertEqual(manifest["evidenceMarker"], "SYNTHETIC_FIXTURE_ONLY")
        paths = sorted(item["path"] for item in manifest["files"])
        self.assertEqual(
            paths,
            sorted([
                "config.json",
                "database/board.sqlite3",
                "catalog/catalog.json",
                "catalog/" + MATERIAL_SNAPSHOT,
            ]),
        )
        self.assertTrue((out / "database" / "board.sqlite3").is_file())
        self.assertTrue((out / "catalog" / "catalog.json").is_file())
        self.assertTrue((out / "catalog" / MATERIAL_SNAPSHOT).is_file())
        self.assertTrue((out / "config.json").is_file())
        self.assertEqual(manifest["copiedFileCount"], len(manifest["files"]))
        self.assertEqual(
            manifest["copiedFileBytes"], sum(item["bytes"] for item in manifest["files"])
        )
        self.assertEqual(manifest["boardSha256"], self.board_sha)
        self.assertTrue(rec._is_lower_hex64(manifest["configIdentity"]))
        self.assertTrue(rec._is_lower_hex64(manifest["dataSourceIdentity"]))

    def test_manifest_digests_and_modes_match_disk(self):
        manifest = self.backup()
        for item in manifest["files"]:
            target = Path(manifest["outputRoot"]) / item["path"]
            raw = target.read_bytes()
            self.assertEqual(item["bytes"], len(raw), item["path"])
            self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest(), item["path"])
            self.assertEqual(
                item["mode"], "0%03o" % stat.S_IMODE(os.lstat(target).st_mode), item["path"]
            )
        self.assertEqual(stat.S_IMODE(os.lstat(manifest["outputRoot"]).st_mode), 0o700)

    def test_output_root_contains_only_expected_entries(self):
        manifest = self.backup()
        self.assertEqual(
            sorted(os.listdir(manifest["outputRoot"])),
            ["catalog", "config.json", "database", "manifest.json"],
        )

    def test_source_identities_are_recorded(self):
        manifest = self.backup()
        db_source = manifest["sources"]["db"]
        self.assertTrue(db_source["present"])
        self.assertTrue(rec._is_lower_hex64(db_source["metadata"]["pathSha256"]))
        self.assertIsNone(db_source["contentSha256"])
        catalog_source = manifest["sources"]["catalog"]
        self.assertTrue(catalog_source["present"])
        self.assertTrue(rec._is_lower_hex64(catalog_source["contentSha256"]))
        # 清单只记录身份摘要，不记录来源原始路径。
        blob = json.dumps(manifest, ensure_ascii=False)
        self.assertNotIn(str(self.db_path), blob)
        self.assertNotIn(str(self.catalog_path), blob)

    def test_backup_reads_back_equal_through_readonly_apis(self):
        manifest = self.backup()
        backup_db = Path(manifest["outputRoot"]) / "database" / "board.sqlite3"
        self.assertEqual(
            self._read_rows_via_store(backup_db), self._read_rows_via_store(self.db_path)
        )

    def test_backup_database_satisfies_full_schema_and_integrity(self):
        manifest = self.backup()
        backup_db = Path(manifest["outputRoot"]) / "database" / "board.sqlite3"
        report = rec._database_report(str(backup_db))
        self.assertTrue(report["present"])
        self.assertTrue(report["schemaVerified"])
        self.assertTrue(report["integrityOk"])
        self.assertGreaterEqual(report["projectIds"], 1)

    def test_config_json_is_canonical_nine_fields(self):
        config = self.prepare_ready()
        manifest = self.backup(config=config)
        raw = (Path(manifest["outputRoot"]) / "config.json").read_bytes()
        expected = json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self.assertEqual(raw, expected)
        self.assertEqual(manifest["configSha256"], hashlib.sha256(raw).hexdigest())

    def test_backup_changes_no_source_and_creates_no_sidecar(self):
        before = _fingerprint(self._watched_sources())
        manifest = self.backup()
        self.assertEqual(_fingerprint(self._watched_sources()), before)
        self.assertFalse(Path(str(self.db_path) + "-wal").exists())
        self.assertFalse(Path(str(self.db_path) + "-shm").exists())
        self.assertFalse(Path(str(self.db_path) + "-journal").exists())
        self.assertEqual(sorted(os.listdir(self.artifact_root)), [OUTPUT_NAME])
        self.assertTrue((Path(manifest["outputRoot"]) / "manifest.json").is_file())

    def test_backup_creates_only_one_new_directory(self):
        neighbour = self.artifact_root / "neighbour"
        neighbour.mkdir()
        (neighbour / "keep.txt").write_text("keep", encoding="utf-8")
        self.backup()
        self.assertEqual(sorted(os.listdir(self.artifact_root)), ["D4 recovery", "neighbour"])
        self.assertEqual((neighbour / "keep.txt").read_text(encoding="utf-8"), "keep")

    def test_backup_includes_database_catalog_and_config(self):
        config = self.prepare_ready()
        manifest = self.backup(config=config)
        paths = sorted(item["path"] for item in manifest["files"])
        self.assertIn("database/board.sqlite3", paths)
        self.assertIn("catalog/catalog.json", paths)
        self.assertIn("config.json", paths)
        roles = {item["role"] for item in manifest["files"]}
        self.assertEqual(roles, {"database", "catalog", "material", "config"})

    def test_backup_is_refused_when_output_already_exists(self):
        self.prepare_ready()
        existing = self.artifact_root / OUTPUT_NAME
        existing.mkdir()
        sentinel = existing / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        before = _fingerprint([existing, sentinel])
        with self.assertRaises(rec.RecoveryError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(_fingerprint([existing, sentinel]), before)


# --------------------------------------------------------------------------- #
# 只读校验
# --------------------------------------------------------------------------- #
@requires_python
class VerifyTests(RecoveryFixtureTestCase):
    def setUp(self):
        super().setUp()
        self.manifest = self.backup()
        self.snapshot_root = Path(self.manifest["outputRoot"])

    def _copy_snapshot(self, name="copy"):
        target = self._sandbox / name
        shutil.copytree(self.snapshot_root, target)
        return target

    def _rewrite_manifest(self, root, mutate):
        path = root / "manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        mutate(manifest)
        path.write_bytes(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        )

    def _overwrite_file_and_manifest(self, root, relative, data):
        (root / relative).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()

        def mutate(manifest):
            for item in manifest["files"]:
                if item["path"] == relative:
                    item["sha256"] = digest
                    item["bytes"] = len(data)
            manifest["copiedFileBytes"] = sum(item["bytes"] for item in manifest["files"])
        self._rewrite_manifest(root, mutate)

    def test_valid_snapshot_passes(self):
        result = rec.verify_snapshot(str(self.snapshot_root))
        self.assertTrue(result["valid"])
        self.assertEqual(result["reason"], "ok")
        self.assertTrue(result["databaseVerified"])
        self.assertTrue(result["catalogVerified"])
        self.assertEqual(result["fileCount"], 4)

    def test_verify_is_readonly(self):
        before = _fingerprint([self.snapshot_root] + [self.snapshot_root / i["path"] for i in self.manifest["files"]])
        rec.verify_snapshot(str(self.snapshot_root))
        self.assertEqual(
            _fingerprint([self.snapshot_root] + [self.snapshot_root / i["path"] for i in self.manifest["files"]]),
            before,
        )

    def test_absent_snapshot_root_is_rejected(self):
        result = rec.verify_snapshot(str(self._sandbox / "missing"))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "snapshot_missing")

    @requires_symlink
    def test_symlink_snapshot_root_is_rejected(self):
        link = self._sandbox / "snapshot-link"
        os.symlink(str(self.snapshot_root), str(link))
        result = rec.verify_snapshot(str(link))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "snapshot_symlink")

    def test_malformed_manifest_is_rejected(self):
        root = self._copy_snapshot()
        (root / "manifest.json").write_bytes(b"{not-json")
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "manifest_malformed")

    def test_incomplete_manifest_is_rejected(self):
        root = self._copy_snapshot()
        self._rewrite_manifest(root, lambda manifest: manifest.pop("files"))
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "manifest_incomplete")

    def test_manifest_with_bad_file_entry_is_rejected(self):
        root = self._copy_snapshot()

        def mutate(manifest):
            manifest["files"][0]["sha256"] = "not-a-digest"
        self._rewrite_manifest(root, mutate)
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "manifest_incomplete")

    def test_missing_file_is_rejected(self):
        root = self._copy_snapshot()
        (root / "database" / "board.sqlite3").unlink()
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "file_missing")

    def test_changed_file_is_rejected(self):
        root = self._copy_snapshot()
        raw = (root / "catalog" / "catalog.json").read_bytes()
        (root / "catalog" / "catalog.json").write_bytes(raw + b"\n")
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "file_changed")

    def test_extra_file_is_rejected(self):
        root = self._copy_snapshot()
        (root / "unrelated.txt").write_text("unrelated", encoding="utf-8")
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "file_extra")

    @requires_symlink
    def test_symlink_file_is_rejected(self):
        root = self._copy_snapshot()
        elsewhere = self._sandbox / "elsewhere.json"
        elsewhere.write_text("{}", encoding="utf-8")
        target = root / "catalog" / "catalog.json"
        target.unlink()
        os.symlink(str(elsewhere), str(target))
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "file_symlink")

    def test_corrupt_database_bytes_are_rejected(self):
        root = self._copy_snapshot()
        (root / "database" / "board.sqlite3").write_bytes(b"not a sqlite database at all")
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "file_changed")

    def test_wrong_schema_database_with_matching_manifest_is_rejected(self):
        root = self._copy_snapshot()
        placeholder = self._sandbox / "placeholder.sqlite3"
        connection = sqlite3.connect(str(placeholder))
        try:
            connection.execute("CREATE TABLE placeholder (value TEXT)")
            connection.commit()
        finally:
            connection.close()
        self._overwrite_file_and_manifest(
            root, "database/board.sqlite3", placeholder.read_bytes()
        )
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "database_invalid")

    def test_missing_material_copy_is_rejected(self):
        root = self._copy_snapshot()
        (root / "catalog" / MATERIAL_SNAPSHOT).unlink()
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertIn(result["reason"], ("file_missing", "catalog_invalid"))

    def test_changed_catalog_with_matching_manifest_is_rejected(self):
        root = self._copy_snapshot()
        tampered = catalog_document(
            documents=[material_document(MATERIAL_SNAPSHOT, MATERIAL_BYTES)]
        )
        # 篡改声明的 sha256（材料拷贝保持不变），并同步清单摘要以绕过逐文件摘要检查。
        tampered["projects"][0]["tasks"][0]["documents"][0]["sha256"] = "0" * 64
        self._overwrite_file_and_manifest(
            root,
            "catalog/catalog.json",
            json.dumps(tampered, ensure_ascii=False, sort_keys=True).encode("utf-8"),
        )
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "catalog_invalid")


# --------------------------------------------------------------------------- #
# 克隆
# --------------------------------------------------------------------------- #
@requires_python
class CloneTests(RecoveryFixtureTestCase):
    def setUp(self):
        super().setUp()
        self.manifest = self.backup()
        self.snapshot_root = Path(self.manifest["outputRoot"])

    def test_clone_produces_a_new_directory_and_verifies(self):
        before = _fingerprint([self.snapshot_root] + [self.snapshot_root / i["path"] for i in self.manifest["files"]])
        clone = self.clone(self.snapshot_root)
        clone_root = Path(clone["outputRoot"])
        self.assertNotEqual(clone_root, self.snapshot_root)
        self.assertEqual(clone_root, self.artifact_root / CLONE_NAME)
        self.assertEqual(clone["operation"], "clone")
        self.assertTrue(rec._is_lower_hex64(clone["sourceManifestDigest"]))
        self.assertTrue(rec.verify_snapshot(str(clone_root))["valid"])
        self.assertEqual(
            sorted(os.listdir(self.artifact_root)),
            sorted([OUTPUT_NAME, CLONE_NAME]),
        )
        # 来源快照在克隆前后未被改动。
        self.assertEqual(
            _fingerprint([self.snapshot_root] + [self.snapshot_root / i["path"] for i in self.manifest["files"]]),
            before,
        )

    def test_clone_database_is_integrity_ok_and_state_equivalent(self):
        clone = self.clone(self.snapshot_root)
        clone_db = Path(clone["outputRoot"]) / "database" / "board.sqlite3"
        snapshot_db = self.snapshot_root / "database" / "board.sqlite3"
        self.assertTrue(rec._integrity_ok(rec._open_readonly_db(str(clone_db))))
        self.assertEqual(rec._database_state(str(clone_db)), rec._database_state(str(snapshot_db)))

    def test_clone_reads_back_equal_through_readonly_apis(self):
        clone = self.clone(self.snapshot_root)
        clone_db = Path(clone["outputRoot"]) / "database" / "board.sqlite3"
        self.assertEqual(self._read_rows_via_store(clone_db), self._read_rows_via_store(self.db_path))

    def test_clone_refuses_existing_output(self):
        (self.artifact_root / CLONE_NAME).mkdir()
        with self.assertRaises(rec.RecoveryError) as caught:
            self.clone(self.snapshot_root)
        self.assertEqual(caught.exception.code, "output_exists")

    def test_clone_refuses_invalid_snapshot(self):
        (self.snapshot_root / "database" / "board.sqlite3").write_bytes(b"broken")
        with self.assertRaises(rec.RecoveryError) as caught:
            self.clone(self.snapshot_root)
        self.assertEqual(caught.exception.code, "snapshot_invalid")

    def test_clone_refuses_output_inside_snapshot(self):
        with self.assertRaises(rec.RecoveryError) as caught:
            self.clone(self.snapshot_root, artifact_root=self.snapshot_root)
        self.assertEqual(caught.exception.code, "output_invalid")

    def test_clone_refuses_temporary_or_installation_artifact_root(self):
        home = os.path.expanduser("~")
        for root in ("/tmp", home + "/Library/LaunchAgents", "/Applications"):
            with self.subTest(root=root):
                with self.assertRaises(rec.RecoveryError) as caught:
                    self.clone(self.snapshot_root, artifact_root=root)
                self.assertEqual(caught.exception.code, "artifact_root_invalid")

    def test_clone_does_not_touch_sources(self):
        before = _fingerprint(self._watched_sources())
        self.clone(self.snapshot_root)
        self.assertEqual(_fingerprint(self._watched_sources()), before)

    def test_clone_manifest_may_differ_in_physical_bytes_from_source_db(self):
        # 不假设物理字节相同：只断言含义等价（schema / 行数 / 行内容摘要）。
        clone = self.clone(self.snapshot_root)
        clone_db = Path(clone["outputRoot"]) / "database" / "board.sqlite3"
        source_db = self.snapshot_root / "database" / "board.sqlite3"
        self.assertIsNotNone(rec._database_state(str(clone_db)))
        self.assertEqual(rec._database_state(str(clone_db)), rec._database_state(str(source_db)))


# --------------------------------------------------------------------------- #
# 负面：来源 / 输出位置 / 漂移
# --------------------------------------------------------------------------- #
@requires_python
class RefuseInvalidSourceTests(RecoveryFixtureTestCase):
    def test_missing_source_is_refused(self):
        self.prepare_ready()
        self.db_path.unlink()
        with self.assertRaises(rec.RecoveryError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, "config_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_unready_database_is_refused(self):
        self.write_database(schema=False, seed=False)
        self.write_catalog()
        with self.assertRaises(rec.RecoveryError) as caught:
            self.backup()
        self.assertEqual(caught.exception.code, "source_not_ready")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_wal_advertising_source_is_refused(self):
        self.prepare_ready()
        raw = bytearray(self.db_path.read_bytes())
        raw[18] = 2
        raw[19] = 2
        self.db_path.write_bytes(bytes(raw))
        with self.assertRaises(rec.RecoveryError) as caught:
            self.backup()
        self.assertIn(caught.exception.code, ("source_not_ready", "source_unreadable"))
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_board_sha_mismatch_is_refused(self):
        self.prepare_ready()
        with self.assertRaises(rec.RecoveryError) as caught:
            rec.backup_snapshot(
                self.config(),
                allowed_root=str(self.root),
                expected_board_sha256="0" * 64,
                artifact_root=str(self.artifact_root),
                output_name="nope",
            )
        self.assertEqual(caught.exception.code, "config_invalid")

    def test_output_name_must_be_a_single_safe_segment(self):
        self.prepare_ready()
        for name in ("", ".", "..", "a/b", "/absolute", "nested/child", "a\x00b", "a\nb"):
            with self.subTest(name=repr(name)):
                with self.assertRaises(rec.RecoveryError) as caught:
                    self.backup(output_name=name)
                self.assertEqual(caught.exception.code, "output_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_temporary_or_installing_artifact_root_is_refused(self):
        self.prepare_ready()
        home = os.path.expanduser("~")
        for root in (
            "/tmp",
            "/private/tmp",
            "/var/folders/aa/bb",
            home + "/Library/LaunchAgents",
            home + "/Applications",
            "/Library/LaunchAgents",
            "/Applications",
            "relative/root",
            str(self.root / "missing-artifacts"),
        ):
            with self.subTest(root=root):
                with self.assertRaises(rec.RecoveryError) as caught:
                    self.backup(artifact_root=root)
                self.assertEqual(caught.exception.code, "artifact_root_invalid")

    @requires_symlink
    def test_symlinked_artifact_root_is_refused(self):
        self.prepare_ready()
        alias = self.root / "artifact-alias"
        os.symlink(str(self.artifact_root), str(alias))
        with self.assertRaises(rec.RecoveryError) as caught:
            self.backup(artifact_root=alias)
        self.assertEqual(caught.exception.code, "artifact_root_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_source_identity_drift_is_refused(self):
        self.prepare_ready()
        original = rec._db_file_identity
        calls = {"count": 0}

        def flaky(path):
            result = original(path)
            calls["count"] += 1
            if calls["count"] >= 2 and result is not None:
                return dict(result, inode=result["inode"] + 1)
            return result

        with mock.patch.object(rec, "_db_file_identity", side_effect=flaky):
            with self.assertRaises(rec.RecoveryError) as caught:
                self.backup()
        self.assertEqual(caught.exception.code, "source_drift")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_catalog_drift_is_refused(self):
        self.prepare_ready()
        original = rec._read_catalog_state
        first = original(str(self.catalog_path))
        second = dict(first, digest="0" * 64, materials=())
        with mock.patch.object(rec, "_read_catalog_state", side_effect=[first, second]):
            with self.assertRaises(rec.RecoveryError) as caught:
                self.backup()
        self.assertEqual(caught.exception.code, "source_drift")
        self.assertEqual(os.listdir(self.artifact_root), [])


# --------------------------------------------------------------------------- #
# 无进程 / 无网络
# --------------------------------------------------------------------------- #
@requires_python
class IsolationTests(RecoveryFixtureTestCase):
    def test_backup_verify_clone_use_no_process_and_no_network(self):
        self.prepare_ready()
        with mock.patch("socket.socket", side_effect=AssertionError("network trap")) as net, \
                mock.patch("subprocess.Popen", side_effect=AssertionError("process trap")) as popen, \
                mock.patch("subprocess.run", side_effect=AssertionError("process trap")) as run, \
                mock.patch("os.system", side_effect=AssertionError("shell trap")) as shell:
            manifest = self.backup()
            rec.verify_snapshot(manifest["outputRoot"])
            self.clone(manifest["outputRoot"])
        self.assertFalse(net.called)
        self.assertFalse(popen.called)
        self.assertFalse(run.called)
        self.assertFalse(shell.called)


# --------------------------------------------------------------------------- #
# 合成“重启后可恢复”：用已接受读取器 freeze 新克隆路径 + collect
# --------------------------------------------------------------------------- #
@requires_python
class RestoredReadinessTests(RecoveryFixtureTestCase):
    def test_cloned_sources_are_ready_through_the_accepted_reader(self):
        sources_before = _fingerprint(self._watched_sources())
        manifest = self.backup()
        clone = self.clone(manifest["outputRoot"])
        clone_root = Path(clone["outputRoot"])

        restored_config = self.config(
            dbPath=str(clone_root / "database" / "board.sqlite3"),
            catalogPath=str(clone_root / "catalog" / "catalog.json"),
        )
        runtime = _load("d4_health_runtime_recovery_tests", _SCRIPTS_DIR / "d4_health_runtime.py")
        frozen = runtime.freeze_identity(restored_config, expected_board_sha256=self.board_sha)
        payload = runtime.collect_health(frozen)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["reason"], "ok")
        self.assertEqual(self._read_rows_via_store(clone_root / "database" / "board.sqlite3"),
                         self._read_rows_via_store(self.db_path))
        # 真实 macOS 登录 / 崩溃恢复 / 双击 / 重启 / 日常迁移：NOT_RUN（本 worker 未执行）。
        self.assertEqual(_fingerprint(self._watched_sources()), sources_before)


# --------------------------------------------------------------------------- #
# 静态表面：无网络 / 无进程 / 无 shell / 固定公共 API 与错误码
# --------------------------------------------------------------------------- #
class StaticSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.source = (_SCRIPTS_DIR / "d4_app_recovery.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_network_or_process_imports(self):
        forbidden = {
            "socket", "subprocess", "http", "urllib", "ssl", "ftplib", "asyncio",
            "multiprocessing", "ctypes", "requests", "webbrowser", "launchd",
        }
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & forbidden, set())

    def test_no_shell_service_or_install_token_in_source(self):
        for token in ("launchctl", "bootstrap", "bootout", "kickstart", "os.system", "popen"):
            self.assertNotIn(token, self.source, token)

    def test_no_command_line_entry_point(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.If):
                names = {child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)}
                self.assertNotIn("__name__", names, "候选模块不得包含 __main__ 入口")

    def test_public_api_is_exactly_the_contract(self):
        self.assertTrue(callable(rec.backup_snapshot))
        self.assertTrue(callable(rec.verify_snapshot))
        self.assertTrue(callable(rec.clone_snapshot))
        self.assertTrue(issubclass(rec.RecoveryError, Exception))
        self.assertEqual(
            sorted(rec.__all__),
            ["RecoveryError", "backup_snapshot", "clone_snapshot", "verify_snapshot"],
        )

    def test_error_codes_and_reasons_are_bounded(self):
        for code in rec.RECOVERY_ERROR_CODES:
            self.assertEqual(str(rec.RecoveryError(code)), code)
        with self.assertRaises(ValueError):
            rec.RecoveryError("definitely-not-a-code")
        result = rec.verify_snapshot("/definitely/not/a/snapshot/here")
        self.assertFalse(result["valid"])
        self.assertIn(result["reason"], rec.VERIFY_REASONS)

    def test_no_source_path_is_persisted_in_source_code(self):
        # 模块只使用调用方传入的路径，不含任何硬编码来源 / fixture 路径。
        self.assertNotIn("test-artifacts", self.source)
        self.assertNotIn(str(_TESTS_DIR), self.source)


if __name__ == "__main__":
    unittest.main()
