import json
import sqlite3
from pathlib import Path
from unittest import mock
from test_d4_app_recovery import RecoveryFixtureTestCase, rec

class RecoveryBoundaryTests(RecoveryFixtureTestCase):
    def setUp(self):
        super().setUp()
        self.prepare_ready()

    def test_empty_snapshot_is_not_valid(self):
        manifest = self.backup()
        root = Path(manifest["outputRoot"])
        for item in manifest["files"]:
            (root / item["path"]).unlink()
        manifest.update(files=[], copiedFileCount=0, copiedFileBytes=0)
        (root / "manifest.json").write_text(json.dumps(manifest))
        self.assertFalse(rec.verify_snapshot(str(root))["valid"])

    def test_missing_declared_database_is_not_valid(self):
        manifest = self.backup()
        root = Path(manifest["outputRoot"])
        removed = [item for item in manifest["files"] if item["role"] == rec.ROLE_DATABASE]
        for item in removed:
            (root / item["path"]).unlink()
        manifest["files"] = [item for item in manifest["files"] if item not in removed]
        manifest["copiedFileCount"] = len(manifest["files"])
        manifest["copiedFileBytes"] = sum(item["bytes"] for item in manifest["files"])
        (root / "manifest.json").write_text(json.dumps(manifest))
        self.assertFalse(rec.verify_snapshot(str(root))["valid"])

    def test_every_readonly_connection_has_vm_budget(self):
        with mock.patch.object(rec._HEALTH_RUNTIME, "MAX_SQLITE_VM_STEPS", 50):
            connection = rec._open_readonly_db(str(self.db_path))
            try:
                with self.assertRaises(sqlite3.Error):
                    connection.execute("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000) SELECT x FROM n").fetchall()
            finally:
                connection.close()

    def test_empty_directory_tree_is_bounded(self):
        manifest = self.backup()
        root = Path(manifest["outputRoot"])
        for index in range(60):
            (root / ("extra" + str(index))).mkdir()
        with mock.patch.object(rec, "MAX_SNAPSHOT_FILES", 8):
            self.assertFalse(rec.verify_snapshot(str(root))["valid"])

    def test_backup_size_refused_before_full_copy(self):
        with mock.patch.object(rec, "MAX_DESTINATION_BYTES", 1), mock.patch.object(rec, "_run_backup", side_effect=AssertionError("must refuse before copying")):
            with self.assertRaises(rec.RecoveryError):
                self.backup()

    def test_incomplete_project_scan_cannot_verify_snapshot(self):
        manifest = self.backup()
        root = Path(manifest["outputRoot"])
        db_item = next(item for item in manifest["files"] if item["role"] == rec.ROLE_DATABASE)
        database = root / db_item["path"]
        connection = sqlite3.connect(str(database))
        try:
            connection.executemany("INSERT INTO workspace_projects(project_id,name,goal,created_at,updated_at) VALUES (?,?,?,?,?)", [("extra-%04d" % i,"fixture","fixture","fixture","fixture") for i in range(1025)])
            connection.commit()
        finally:
            connection.close()
        import hashlib
        raw = database.read_bytes()
        db_item["bytes"] = len(raw)
        db_item["sha256"] = hashlib.sha256(raw).hexdigest()
        manifest["copiedFileBytes"] = sum(item["bytes"] for item in manifest["files"])
        (root / "manifest.json").write_text(json.dumps(manifest))
        result = rec.verify_snapshot(str(root))
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "database_invalid")
