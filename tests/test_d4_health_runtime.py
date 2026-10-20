"""Independent unittest suite for candidate/scripts/d4_health_runtime.py (G2-A collector).

NOT_RUN in this attempt
-----------------------
This worker has ``Bash=[]``: no command was executed, no test was run and no SHA was
recomputed in this round. Every test result below is therefore NOT_RUN, not PASS; the
actual run and the independent scope/SHA verification belong to cox.

Fixtures
--------
Every fixture is created inside one temporary directory made by ``tempfile.mkdtemp`` for
the single test and removed again in cleanup. The frozen protocol rejects configured
paths under ``/tmp``, ``/private/tmp``, ``/var/folders`` and ``/private/var/folders``,
so that temporary directory is created under the candidate directory (this file's
parent's parent) instead of under the system temp dir. No fixture is ever created
outside that directory and no test touches any configured runtime path, real database,
launch agent or service.

Positive database fixtures execute the complete preplaced frozen schema fixture
``candidate/tests/fixtures/d4_readiness_schema.sql`` (read-only, never modified or
copied) so the collector sees the real D4 storage contract; catalog material fixtures
are written inside the same temporary directory under the catalog parent. The frozen
schema fixture bytes are never changed by this suite.

The candidate module and the frozen reader module are imported from their exact
relative file paths via importlib file specs; there is no directory scan, no environment
override and no import of the frozen input modules. The only directory listings in this
file are of the test's own temporary fixture directory, used to prove that the collector
creates nothing.

Regressions added by this rework round
--------------------------------------
* ``SqliteStepBudgetTests`` exercises the shared finite VM-step budget: the same frozen
  identity and the same fixture flip from ready to fail-closed only because the budget
  shrinks, and a deliberately duplicate-heavy ``tasks`` table -- which a ``LIMIT`` cannot
  bound -- is bounded by that budget.
* ``CollectHealthTests.test_project_scan_select_failure_is_never_ready`` no longer damages
  ``workspace_projects``: its ``DISTINCT`` scan is served by a covering PRIMARY KEY index,
  so the damaged page was never read and the earlier fixture could not fail honestly. It
  now damages ``tasks``, whose ``project_id`` has no covering index, asserts with
  ``EXPLAIN QUERY PLAN`` that the collector's scan really reads the table's b-tree, proves
  that the same SELECT really raises, and still requires a fail-closed, never-ready payload.
* ``CatalogMaterialTests`` adds a real internal-directory alias symlink (a symlink ancestor
  resolving back *inside* the catalog parent), NUL/control-character and lone-surrogate
  material paths and a valid nested regular path, and asserts that no payload ever leaks a
  path.
"""

import dataclasses
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load candidate module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rt = _load("d4_health_runtime", "d4_health_runtime.py")
d4r = _load("d4_health_reader_for_runtime_tests", "d4_health_reader.py")


# --------------------------------------------------------------------------- #
# Independent normative formula and literals (does NOT call the implementation)
# --------------------------------------------------------------------------- #
CONFIG_FIELD_NAMES = (
    "schemaVersion",
    "host",
    "port",
    "dbPath",
    "catalogPath",
    "pythonPath",
    "boardScript",
    "runtimeDir",
    "launchAgentLabel",
)

HEALTH_FIELDS = (
    "schemaVersion",
    "service",
    "boardSha256",
    "configIdentity",
    "dataSource",
    "ready",
    "reason",
    "auth",
)

DATA_SOURCE_FIELDS = ("kind", "present", "identity")

REASONS = (
    "ok",
    "source_missing",
    "source_replaced",
    "source_unreadable",
    "source_changed",
    "catalog_invalid",
    "catalog_changed",
    "database_invalid",
    "project_collision",
    "board_sha_unavailable",
)

COLLECT_REASONS = (
    "ok",
    "source_missing",
    "source_unreadable",
    "source_replaced",
    "database_invalid",
    "catalog_invalid",
    "catalog_changed",
    "project_collision",
    "board_sha_unavailable",
)

LOWER_HEX = "0123456789abcdef"

BOARD_SCRIPT_SOURCE = '"""D4 board test double (fixture only)."""\nprint("fixture")\n'

FIXTURE_BASE = Path(__file__).resolve().parents[1]

# The complete preplaced D4 schema fixture (read-only manifest input). Positive database
# fixtures execute it verbatim instead of synthesising incomplete fake tables.
SCHEMA_FIXTURE_PATH = FIXTURE_BASE / "tests" / "fixtures" / "d4_readiness_schema.sql"


def independent_canonical_bytes(obj):
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def independent_digest(obj):
    return hashlib.sha256(independent_canonical_bytes(obj)).hexdigest()


def independent_path_digest(path):
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def _is_plain_identifier(name):
    """True for a plain SQL identifier safe to interpolate into a test-only PRAGMA."""
    return (
        isinstance(name, str)
        and name != ""
        and not name[0].isdigit()
        and all(char.isalnum() or char == "_" for char in name)
    )


def independent_config_identity(config, board_sha256):
    fields = {name: config[name] for name in CONFIG_FIELD_NAMES}
    return independent_digest(
        {
            "schemaVersion": "d4-config-identity.v1",
            "boardSha256": board_sha256,
            "config": fields,
        }
    )


def independent_data_source_identity(db, catalog):
    def clean(meta):
        if meta is None:
            return None
        return {
            "pathSha256": meta["pathSha256"],
            "device": meta["device"],
            "inode": meta["inode"],
        }

    return independent_digest(
        {
            "schemaVersion": "d4-data-source-identity.v1",
            "db": clean(db),
            "catalog": clean(catalog),
        }
    )


def independent_expected_payload(*, board_sha, config_identity, kind, present, identity, ready, reason):
    return {
        "schemaVersion": "d4-board-health.v1",
        "service": "d4-board",
        "boardSha256": board_sha,
        "configIdentity": config_identity,
        "dataSource": {"kind": kind, "present": present, "identity": identity},
        "ready": ready,
        "reason": reason,
        "auth": "none",
    }


def observed_metadata(path):
    info = os.lstat(str(path))
    return {
        "pathSha256": independent_path_digest(str(path)),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


def catalog_document(*, project_id="d4-product", name="D4 看板", task_id="task-1", status="verified", documents=None):
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


def material_document(snapshot, content, *, name="doc.md", kind="markdown", source_path=None):
    """A catalog document entry pointing at one catalog material snapshot."""
    return {
        "name": name,
        "kind": kind,
        "snapshot": snapshot,
        "sha256": hashlib.sha256(content).hexdigest(),
        "sourcePath": ("archive/" + name) if source_path is None else source_path,
    }


@dataclasses.dataclass(frozen=True)
class AppConfigStub:
    """Stand-in for the G1 ``AppConfig`` frozen dataclass (same nine attribute names)."""

    schema_version: str
    host: str
    port: int
    db_path: object
    catalog_path: object
    python_path: str
    board_script: str
    runtime_dir: str
    label: str


class RuntimeFixtureTestCase(unittest.TestCase):
    def setUp(self):
        self.root = Path(
            tempfile.mkdtemp(prefix="d4-health-runtime-fixtures-", dir=str(FIXTURE_BASE))
        )
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.app_dir = self.root / "app"
        self.app_dir.mkdir()
        self.board_script = self.app_dir / "d4_board.py"
        self.board_script.write_text(BOARD_SCRIPT_SOURCE, encoding="utf-8")
        self.board_sha = hashlib.sha256(BOARD_SCRIPT_SOURCE.encode("utf-8")).hexdigest()
        self.db_path = self.root / "board.sqlite3"
        self.catalog_path = self.root / "catalog.json"
        self.runtime_dir = self.root / "runtime"
        self.runtime_dir.mkdir()

    # -- fixture writers -----------------------------------------------------
    def write_database(self, path=None, *, projects=(), extra_tasks=(), schema=True):
        target = Path(path) if path is not None else self.db_path
        if target.is_dir():
            shutil.rmtree(str(target))
        elif target.exists() or target.is_symlink():
            target.unlink()
        connection = sqlite3.connect(str(target))
        try:
            if schema:
                connection.executescript(SCHEMA_FIXTURE_PATH.read_text(encoding="utf-8"))
            else:
                # A valid SQLite file that lacks the required D4 schema.
                connection.execute("CREATE TABLE placeholder (value TEXT)")
            for project_id in projects:
                connection.execute(
                    "INSERT INTO workspace_projects "
                    "(project_id, name, goal, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        project_id,
                        project_id,
                        project_id,
                        "2026-01-01T00:00:00Z",
                        "2026-01-01T00:00:00Z",
                    ),
                )
            for task_id, project_id in extra_tasks:
                connection.execute(
                    "INSERT INTO tasks (task_id, project_id) VALUES (?, ?)",
                    (task_id, project_id),
                )
            connection.commit()
        finally:
            connection.close()
        return target

    def write_material(self, relative, content):
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return target

    def corrupt_table_page(self, path, table):
        """Overwrite one table's b-tree root page so a SELECT on it fails (synthetic DB)."""
        connection = sqlite3.connect(str(path))
        try:
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            row = connection.execute(
                "SELECT rootpage FROM sqlite_master WHERE name = ?", (table,)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        rootpage = int(row[0])
        self.assertGreater(rootpage, 1)
        with open(str(path), "r+b") as handle:
            handle.seek((rootpage - 1) * page_size)
            handle.write(b"\xff" * page_size)
        return rootpage

    def drop_indexes_covering(self, path, table, column):
        """Drop test-fixture indexes on ``table`` that cover ``column`` (autoindexes stay).

        The preplaced schema fixture defines no index on ``tasks(project_id)``; this keeps
        the damaged-page fixture honest if that ever changes.
        """
        dropped = []
        connection = sqlite3.connect(str(path))
        try:
            names = [
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
                    (table,),
                ).fetchall()
                if isinstance(row[0], str) and row[0].startswith("sqlite_autoindex_") is False
            ]
            for name in names:
                if not _is_plain_identifier(name):
                    continue
                columns = [
                    row[2]
                    for row in connection.execute(
                        "PRAGMA index_info('%s')" % name
                    ).fetchall()
                ]
                if column in columns:
                    connection.execute('DROP INDEX "%s"' % name)
                    dropped.append(name)
            connection.commit()
        finally:
            connection.close()
        return tuple(dropped)

    def project_scan_plan(self, path, table):
        """``EXPLAIN QUERY PLAN`` detail text of the collector's project-id scan."""
        connection = sqlite3.connect(str(path))
        try:
            rows = connection.execute(
                "EXPLAIN QUERY PLAN SELECT DISTINCT project_id FROM %s LIMIT ?" % table,
                (rt.MAX_IDENTITY_ROWS + 1,),
            ).fetchall()
        finally:
            connection.close()
        return " | ".join(str(row[3]) for row in rows)

    def assert_project_scan_reads_the_table(self, path, table):
        """The scan used by the collector must read the table b-tree, not a covering index."""
        plan = self.project_scan_plan(path, table)
        self.assertIn(table, plan)
        self.assertNotIn("COVERING INDEX", plan)

    def assert_project_scan_raises(self, path, table):
        """Prove that the collector's project-scan SQL really fails on the damaged fixture."""
        connection = sqlite3.connect(str(path))
        try:
            with self.assertRaises(sqlite3.Error):
                connection.execute(
                    "SELECT DISTINCT project_id FROM %s LIMIT ?" % table,
                    (rt.MAX_IDENTITY_ROWS + 1,),
                ).fetchall()
        finally:
            connection.close()

    def write_catalog(self, path=None, document=None):
        target = Path(path) if path is not None else self.catalog_path
        payload = catalog_document() if document is None else document
        target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        return target

    def config(self, *, db=True, catalog=True, **overrides):
        config = {
            "schemaVersion": "d4-board-app-config.v1",
            "host": "127.0.0.1",
            "port": 8787,
            "dbPath": str(self.db_path) if db else None,
            "catalogPath": str(self.catalog_path) if catalog else None,
            "pythonPath": "/usr/bin/python3",
            "boardScript": str(self.board_script),
            "runtimeDir": str(self.runtime_dir),
            "launchAgentLabel": "com.conductor.d4.board",
        }
        config.update(overrides)
        return config

    def app_config_object(self, **overrides):
        config = self.config(**overrides)
        return AppConfigStub(
            schema_version=config["schemaVersion"],
            host=config["host"],
            port=config["port"],
            db_path=config["dbPath"],
            catalog_path=config["catalogPath"],
            python_path=config["pythonPath"],
            board_script=config["boardScript"],
            runtime_dir=config["runtimeDir"],
            label=config["launchAgentLabel"],
        )

    def prepare_ready(self):
        """Create a non-colliding valid fixture: live projects differ from the catalog id."""
        self.write_database(
            projects=("d4-product-live",), extra_tasks=(("task-live-1", "d4-product-live"),)
        )
        self.write_catalog(document=catalog_document(project_id="d4-product"))
        return self.config()

    # -- fixture observations (test-owned temporary directory only) ----------
    def fixture_listing(self):
        listing = {}
        for directory in (self.root, self.app_dir):
            listing[str(directory.relative_to(self.root))] = sorted(os.listdir(str(directory)))
        return listing

    def file_state(self, path):
        info = os.lstat(str(path))
        return (
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        )

    def frozen_ready(self):
        config = self.prepare_ready()
        return config, rt.freeze_identity(config, expected_board_sha256=self.board_sha)


# --------------------------------------------------------------------------- #
# Step 1: freeze_identity
# --------------------------------------------------------------------------- #
class FreezeIdentityTests(RuntimeFixtureTestCase):
    def test_freeze_reports_the_expected_frozen_identity(self):
        config = self.prepare_ready()
        frozen = rt.freeze_identity(config, expected_board_sha256=self.board_sha)

        self.assertEqual(frozen.board_sha256, self.board_sha)
        self.assertEqual(frozen.config_identity, independent_config_identity(config, self.board_sha))
        self.assertEqual(
            frozen.data_source_identity,
            independent_data_source_identity(
                observed_metadata(self.db_path), observed_metadata(self.catalog_path)
            ),
        )
        self.assertEqual(frozen.config, config)
        self.assertEqual(frozen.board_script["contentSha256"], self.board_sha)
        self.assertEqual(frozen.board_script["pathSha256"], independent_path_digest(self.board_script))
        self.assertEqual(observed_metadata(self.board_script)["inode"], frozen.board_script["inode"])
        self.assertEqual(frozen.sources["db"]["metadata"], observed_metadata(self.db_path))
        self.assertEqual(frozen.sources["catalog"]["metadata"], observed_metadata(self.catalog_path))
        self.assertIsNone(frozen.sources["db"]["contentSha256"])
        self.assertEqual(len(frozen.sources["catalog"]["contentSha256"]), 64)

    def test_freeze_accepts_an_appconfig_like_object(self):
        self.write_database()
        self.write_catalog()
        frozen = rt.freeze_identity(
            self.app_config_object(), expected_board_sha256=self.board_sha
        )
        self.assertEqual(frozen.config, self.config())

    def test_freeze_works_for_each_supported_source_selection(self):
        self.write_database()
        self.write_catalog()
        for label, options, kind in (
            ("db only", {"catalog": False}, "sqlite"),
            ("catalog only", {"db": False}, "catalog"),
            ("both", {}, "sqlite+catalog"),
        ):
            with self.subTest(case=label):
                frozen = rt.freeze_identity(
                    self.config(**options), expected_board_sha256=self.board_sha
                )
                payload = rt.collect_health(frozen)
                self.assertEqual(payload["dataSource"]["kind"], kind)
                self.assertEqual(payload["reason"], "ok")
                self.assertIs(payload["ready"], True)

    def test_frozen_identity_top_level_fields_are_immutable(self):
        _, frozen = self.frozen_ready()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            frozen.board_sha256 = "0" * 64

    def test_freeze_rejects_invalid_configs_with_generic_codes(self):
        self.write_database()
        self.write_catalog()
        cases = {
            "relative db": self.config(dbPath="data/board.sqlite3"),
            "no source": self.config(dbPath=None, catalogPath=None),
            "extra field": dict(self.config(), extra=1),
            "missing field": {k: v for k, v in self.config().items() if k != "port"},
            "board basename": self.config(boardScript=str(self.app_dir / "board.py")),
            "protocol rejects temp db": self.config(dbPath="/tmp/board.sqlite3"),
            "not a mapping": [1, 2, 3],
            "object without the g1 attributes": object(),
        }
        for label, config in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(rt.HealthRuntimeError) as caught:
                    rt.freeze_identity(config, expected_board_sha256=self.board_sha)
                self.assertEqual(caught.exception.code, "invalid_config")
                self.assertEqual(str(caught.exception), "invalid_config")
                self.assertNotIn("/", str(caught.exception))

    def test_freeze_rejects_invalid_expected_board_sha_with_a_generic_code(self):
        self.write_database()
        self.write_catalog()
        for label, value in {
            "uppercase": "A" * 64,
            "short": "a" * 63,
            "nonhex": "z" * 64,
            "bool": True,
            "int": 1,
            "none": None,
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(rt.HealthRuntimeError) as caught:
                    rt.freeze_identity(self.config(), expected_board_sha256=value)
                self.assertEqual(caught.exception.code, "invalid_expected_board_sha256")

    def test_freeze_validates_the_board_script_against_the_expected_digest(self):
        self.write_database()
        self.write_catalog()
        self.board_script.write_text("some other board source\n", encoding="utf-8")
        with self.assertRaises(rt.HealthRuntimeError) as caught:
            rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        self.assertEqual(caught.exception.code, "board_sha_unavailable")
        self.assertNotIn("/", str(caught.exception))

    def test_freeze_fails_closed_when_the_board_script_is_missing(self):
        self.write_database()
        self.write_catalog()
        self.board_script.unlink()
        with self.assertRaises(rt.HealthRuntimeError) as caught:
            rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        self.assertEqual(caught.exception.code, "board_sha_unavailable")
        self.assertFalse(self.board_script.exists())

    def test_freeze_does_not_create_a_missing_database(self):
        self.write_catalog()
        self.assertFalse(self.db_path.exists())
        listing_before = self.fixture_listing()
        with self.assertRaises(rt.HealthRuntimeError) as caught:
            rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        self.assertEqual(caught.exception.code, "source_missing")
        self.assertFalse(self.db_path.exists())
        self.assertEqual(self.fixture_listing(), listing_before)

    def test_freeze_does_not_create_a_missing_catalog(self):
        self.write_database()
        self.assertFalse(self.catalog_path.exists())
        with self.assertRaises(rt.HealthRuntimeError) as caught:
            rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        self.assertEqual(caught.exception.code, "source_missing")
        self.assertFalse(self.catalog_path.exists())

    def test_freeze_reports_an_unreadable_configured_source(self):
        self.write_database()
        self.catalog_path.mkdir()
        with self.assertRaises(rt.HealthRuntimeError) as caught:
            rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        self.assertEqual(caught.exception.code, "source_unreadable")

    def test_freeze_is_read_only(self):
        config = self.prepare_ready()
        listing_before = self.fixture_listing()
        states_before = {
            "db": self.file_state(self.db_path),
            "catalog": self.file_state(self.catalog_path),
            "board": self.file_state(self.board_script),
        }
        frozen = rt.freeze_identity(config, expected_board_sha256=self.board_sha)
        self.assertEqual(frozen.config, config)
        self.assertEqual(self.fixture_listing(), listing_before)
        self.assertEqual(
            {
                "db": self.file_state(self.db_path),
                "catalog": self.file_state(self.catalog_path),
                "board": self.file_state(self.board_script),
            },
            states_before,
        )


# --------------------------------------------------------------------------- #
# Step 2: collect_health
# --------------------------------------------------------------------------- #
class CollectHealthTests(RuntimeFixtureTestCase):
    def assert_contract_payload(self, payload, *, kind, present, identity, ready, reason, frozen):
        self.assertEqual(
            payload,
            independent_expected_payload(
                board_sha=frozen.board_sha256,
                config_identity=frozen.config_identity,
                kind=kind,
                present=present,
                identity=identity,
                ready=ready,
                reason=reason,
            ),
        )
        self.assertEqual(set(payload.keys()), set(HEALTH_FIELDS))
        self.assertEqual(set(payload["dataSource"].keys()), set(DATA_SOURCE_FIELDS))
        self.assertEqual(payload, d4r.validate_health_payload(payload))
        self.assertEqual(payload["auth"], "none")
        self.assertIsInstance(payload["ready"], bool)
        self.assertIsInstance(payload["dataSource"]["present"], bool)
        self.assertIn(payload["reason"], REASONS)
        if present:
            self.assertEqual(len(payload["dataSource"]["identity"]), 64)
            self.assertEqual(
                payload["dataSource"]["identity"], payload["dataSource"]["identity"].lower()
            )
        else:
            self.assertIsNone(payload["dataSource"]["identity"])

    def test_ready_db_only_payload(self):
        self.write_database(projects=("d4-product-live",))
        frozen = rt.freeze_identity(self.config(catalog=False), expected_board_sha256=self.board_sha)
        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="sqlite",
            present=True,
            identity=frozen.data_source_identity,
            ready=True,
            reason="ok",
            frozen=frozen,
        )

    def test_ready_catalog_only_payload(self):
        self.write_catalog()
        frozen = rt.freeze_identity(self.config(db=False), expected_board_sha256=self.board_sha)
        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="catalog",
            present=True,
            identity=frozen.data_source_identity,
            ready=True,
            reason="ok",
            frozen=frozen,
        )

    def test_ready_combined_payload(self):
        _, frozen = self.frozen_ready()
        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="sqlite+catalog",
            present=True,
            identity=frozen.data_source_identity,
            ready=True,
            reason="ok",
            frozen=frozen,
        )

    def test_collect_is_deterministic_and_does_not_change_the_frozen_identity(self):
        _, frozen = self.frozen_ready()
        before = (frozen.board_sha256, frozen.config_identity, frozen.data_source_identity)
        first = rt.collect_health(frozen)
        second = rt.collect_health(frozen)
        self.assertEqual(first, second)
        self.assertEqual(
            (frozen.board_sha256, frozen.config_identity, frozen.data_source_identity), before
        )
        self.assertIsNot(first["dataSource"], second["dataSource"])

    def test_database_content_growth_is_not_a_replacement(self):
        _, frozen = self.frozen_ready()
        before = os.lstat(str(self.db_path))
        connection = sqlite3.connect(str(self.db_path))
        try:
            for index in range(400):
                connection.execute(
                    "INSERT INTO events (event_id, task_id, attempt_id, type) VALUES (?, ?, ?, ?)",
                    ("evt-%d" % index, "task-live-1", "att-1", "task.progress"),
                )
            connection.commit()
        finally:
            connection.close()
        after = os.lstat(str(self.db_path))
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))
        self.assertNotEqual(
            (after.st_size, after.st_mtime_ns), (before.st_size, before.st_mtime_ns)
        )

        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "ok")
        self.assertIs(payload["ready"], True)
        self.assertEqual(payload["dataSource"]["identity"], frozen.data_source_identity)

    def test_in_place_catalog_edit_is_catalog_changed(self):
        _, frozen = self.frozen_ready()
        before = os.lstat(str(self.catalog_path))
        self.write_catalog(document=catalog_document(project_id="d4-product", name="D4 renamed"))
        after = os.lstat(str(self.catalog_path))
        self.assertEqual((after.st_dev, after.st_ino), (before.st_dev, before.st_ino))

        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="sqlite+catalog",
            present=True,
            identity=frozen.data_source_identity,
            ready=False,
            reason="catalog_changed",
            frozen=frozen,
        )

    def test_deleted_database_is_source_missing_and_is_not_recreated(self):
        _, frozen = self.frozen_ready()
        self.db_path.unlink()
        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="sqlite+catalog",
            present=False,
            identity=None,
            ready=False,
            reason="source_missing",
            frozen=frozen,
        )
        self.assertFalse(self.db_path.exists())

    def test_deleted_catalog_is_source_missing_and_is_not_recreated(self):
        _, frozen = self.frozen_ready()
        self.catalog_path.unlink()
        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="sqlite+catalog",
            present=False,
            identity=None,
            ready=False,
            reason="source_missing",
            frozen=frozen,
        )
        self.assertFalse(self.catalog_path.exists())

    def test_replaced_database_is_source_replaced(self):
        _, frozen = self.frozen_ready()
        replacement = self.write_database(
            self.root / "replacement.sqlite3",
            projects=("d4-product-live",),
            extra_tasks=(("task-live-1", "d4-product-live"),),
        )
        os.replace(str(replacement), str(self.db_path))
        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="sqlite+catalog",
            present=True,
            identity=independent_data_source_identity(
                observed_metadata(self.db_path), observed_metadata(self.catalog_path)
            ),
            ready=False,
            reason="source_replaced",
            frozen=frozen,
        )

    def test_replaced_catalog_with_identical_content_is_source_replaced(self):
        _, frozen = self.frozen_ready()
        replacement = self.write_catalog(self.root / "replacement-catalog.json")
        os.replace(str(replacement), str(self.catalog_path))
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "source_replaced")
        self.assertIs(payload["ready"], False)
        self.assertIs(payload["dataSource"]["present"], True)

    def test_database_that_is_not_sqlite_is_database_invalid(self):
        _, frozen = self.frozen_ready()
        self.db_path.write_bytes(b"this is not a sqlite database\x00\x01\x02")
        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="sqlite+catalog",
            present=True,
            identity=frozen.data_source_identity,
            ready=False,
            reason="database_invalid",
            frozen=frozen,
        )

    def test_database_with_only_legacy_core_tables_is_database_invalid(self):
        self.write_catalog()
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute(
                "CREATE TABLE tasks (task_id TEXT PRIMARY KEY, project_id TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE attempts (attempt_id TEXT PRIMARY KEY, task_id TEXT NOT NULL)"
            )
            connection.execute("CREATE TABLE events (event_id TEXT PRIMARY KEY, task_id TEXT)")
            connection.execute(
                "CREATE TABLE deliveries (delivery_id TEXT PRIMARY KEY, task_id TEXT NOT NULL)"
            )
            connection.commit()
        finally:
            connection.close()
        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "database_invalid")
        self.assertIs(payload["ready"], False)

    def test_database_without_the_required_d4_schema_is_database_invalid(self):
        self.write_catalog()
        self.write_database(schema=False)
        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "database_invalid")
        self.assertIs(payload["ready"], False)

    def test_database_missing_a_required_table_is_database_invalid(self):
        self.write_catalog()
        self.write_database(projects=("d4-product-live",))
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute("DROP TABLE workspace_task_links")
            connection.commit()
        finally:
            connection.close()
        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "database_invalid")
        self.assertIs(payload["ready"], False)

    def test_database_missing_a_required_column_is_database_invalid(self):
        self.write_catalog()
        self.write_database(projects=("d4-product-live",))
        connection = sqlite3.connect(str(self.db_path))
        try:
            connection.execute("DROP TABLE tasks")
            connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
            connection.commit()
        finally:
            connection.close()
        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "database_invalid")
        self.assertIs(payload["ready"], False)

    def test_project_scan_overflow_beyond_the_row_bound_is_database_invalid(self):
        # 1024 non-colliding ids plus one colliding id that sorts after the initial page:
        # without a complete (limit-plus-one) DISTINCT scan the collision would be missed
        # and the database would be reported ready.
        self.write_catalog(document=catalog_document(project_id="z-collision"))
        self.write_database(
            projects=tuple("proj-%04d" % index for index in range(rt.MAX_IDENTITY_ROWS))
            + ("z-collision",)
        )
        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "database_invalid")
        self.assertIs(payload["ready"], False)

    def test_project_scan_select_failure_is_never_ready(self):
        # Repaired corruption fixture: ``workspace_projects`` DISTINCT is served by its
        # covering PRIMARY KEY index (``sqlite_autoindex_workspace_projects_1``), so a
        # damaged ``workspace_projects`` page is never read and that fixture could not fail
        # honestly ("ok" was observed). ``tasks`` has no index covering ``project_id``, so
        # the collector's second project scan really reads the table's b-tree page.
        self.write_catalog()
        self.write_database(
            projects=("d4-product-live",),
            extra_tasks=(("task-live-1", "d4-product-live"),),
        )
        self.drop_indexes_covering(self.db_path, "tasks", "project_id")
        self.assert_project_scan_reads_the_table(self.db_path, "tasks")
        self.corrupt_table_page(self.db_path, "tasks")
        # Proof that the damaged page is really consulted: the collector's own scan SQL
        # raises before any ready/fail-closed verdict is asserted.
        self.assert_project_scan_raises(self.db_path, "tasks")

        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "database_invalid")
        self.assertIs(payload["ready"], False)
        self.assertIs(payload["dataSource"]["present"], True)

    def test_wal_database_is_refused_without_creating_sidecars(self):
        connection = sqlite3.connect(str(self.db_path))
        try:
            mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            connection.execute("CREATE TABLE probe (value TEXT)")
            connection.commit()
        finally:
            connection.close()
        if str(mode).lower() != "wal":
            self.skipTest("WAL journalling is unavailable on this filesystem")
        listing_before = self.fixture_listing()
        state_before = self.file_state(self.db_path)
        with self.assertRaises(rt.HealthRuntimeError) as caught:
            rt.freeze_identity(self.config(catalog=False), expected_board_sha256=self.board_sha)
        self.assertEqual(caught.exception.code, "source_unreadable")
        self.assertEqual(self.fixture_listing(), listing_before)
        self.assertEqual(self.file_state(self.db_path), state_before)

    def test_structurally_invalid_catalogs_are_catalog_invalid(self):
        cases = {
            "not json": "{not json",
            "wrong schema": json.dumps({"schemaVersion": "project-catalog.v2", "projects": []}),
            "projects not a list": json.dumps(
                {"schemaVersion": "project-catalog.v1", "projects": {}}
            ),
            "duplicate project id": json.dumps(
                {
                    "schemaVersion": "project-catalog.v1",
                    "projects": [
                        dict(catalog_document()["projects"][0]),
                        dict(catalog_document()["projects"][0]),
                    ],
                }
            ),
            "bad task status": json.dumps(
                catalog_document(status="not-a-status", task_id="task-1")
            ),
            "unknown milestone": json.dumps(
                {
                    "schemaVersion": "project-catalog.v1",
                    "projects": [
                        {
                            "projectId": "d4-product",
                            "name": "D4 看板",
                            "milestones": [{"id": "m-1", "name": "M1"}],
                            "tasks": [
                                {
                                    "taskId": "task-1",
                                    "milestoneId": "m-9",
                                    "status": "verified",
                                    "documents": [],
                                }
                            ],
                            "issues": [],
                        }
                    ],
                }
            ),
            "absolute snapshot": json.dumps(
                {
                    "schemaVersion": "project-catalog.v1",
                    "projects": [
                        {
                            "projectId": "d4-product",
                            "name": "D4 看板",
                            "milestones": [{"id": "m-1", "name": "M1"}],
                            "tasks": [
                                {
                                    "taskId": "task-1",
                                    "milestoneId": "m-1",
                                    "status": "verified",
                                    "documents": [{"snapshot": "/etc/passwd"}],
                                }
                            ],
                            "issues": [],
                        }
                    ],
                }
            ),
        }
        self.write_database()
        for label, text in cases.items():
            with self.subTest(case=label):
                self.catalog_path.write_text(text, encoding="utf-8")
                frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
                payload = rt.collect_health(frozen)
                self.assertEqual(payload["reason"], "catalog_invalid")
                self.assertIs(payload["ready"], False)

    def test_unreadable_sources_are_source_unreadable(self):
        _, frozen = self.frozen_ready()
        self.catalog_path.unlink()
        self.catalog_path.mkdir()
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "source_unreadable")
        self.assertIs(payload["ready"], False)
        self.assertIs(payload["dataSource"]["present"], True)

        self.write_database()
        frozen = rt.freeze_identity(self.config(catalog=False), expected_board_sha256=self.board_sha)
        self.db_path.unlink()
        self.db_path.mkdir()
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "source_unreadable")
        self.assertIs(payload["ready"], False)

    @unittest.skipIf(
        hasattr(os, "geteuid") and os.geteuid() == 0, "root can read any file"
    )
    def test_permission_denied_catalog_is_source_unreadable(self):
        _, frozen = self.frozen_ready()
        os.chmod(str(self.catalog_path), 0o000)
        self.addCleanup(os.chmod, str(self.catalog_path), 0o644)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "source_unreadable")
        self.assertIs(payload["ready"], False)

    def test_project_collision_is_detected(self):
        self.write_catalog(document=catalog_document(project_id="d4-product"))
        cases = {
            "workspace_projects": {"projects": ("d4-product",)},
            "tasks": {"extra_tasks": (("task-live-1", "d4-product"),)},
        }
        for label, options in cases.items():
            with self.subTest(case=label):
                self.write_database(**options)
                frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
                payload = rt.collect_health(frozen)
                self.assertEqual(payload["reason"], "project_collision")
                self.assertIs(payload["ready"], False)
                self.assertIs(payload["dataSource"]["present"], True)

    def test_matching_live_project_ids_do_not_collide(self):
        _, frozen = self.frozen_ready()
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "ok")

    def test_board_script_content_change_is_board_sha_unavailable(self):
        _, frozen = self.frozen_ready()
        self.board_script.write_text("changed board source\n", encoding="utf-8")
        payload = rt.collect_health(frozen)
        self.assert_contract_payload(
            payload,
            kind="sqlite+catalog",
            present=True,
            identity=frozen.data_source_identity,
            ready=False,
            reason="board_sha_unavailable",
            frozen=frozen,
        )

    def test_board_script_deletion_is_board_sha_unavailable(self):
        _, frozen = self.frozen_ready()
        self.board_script.unlink()
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "board_sha_unavailable")
        self.assertIs(payload["ready"], False)
        self.assertFalse(self.board_script.exists())

    def test_board_script_replacement_with_same_content_is_source_replaced(self):
        _, frozen = self.frozen_ready()
        replacement = self.app_dir / "d4_board_replacement.py"
        replacement.write_text(BOARD_SCRIPT_SOURCE, encoding="utf-8")
        os.replace(str(replacement), str(self.board_script))
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "source_replaced")
        self.assertIs(payload["ready"], False)
        self.assertIs(payload["dataSource"]["present"], True)

    def test_source_missing_takes_precedence_over_board_script_drift(self):
        _, frozen = self.frozen_ready()
        self.board_script.write_text("changed board source\n", encoding="utf-8")
        self.db_path.unlink()
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "source_missing")
        self.assertIs(payload["dataSource"]["present"], False)
        self.assertIsNone(payload["dataSource"]["identity"])
        self.assertFalse(self.db_path.exists())

    def test_collect_rejects_an_invalid_frozen_identity(self):
        for label, value in {
            "mapping": {"board_sha256": self.board_sha},
            "none": None,
            "string": "frozen",
            "list": [1, 2],
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(rt.HealthRuntimeError) as caught:
                    rt.collect_health(value)
                self.assertEqual(caught.exception.code, "invalid_frozen_identity")

    def test_collect_creates_nothing_and_writes_nothing(self):
        _, frozen = self.frozen_ready()
        listing_before = self.fixture_listing()
        states_before = {
            "db": self.file_state(self.db_path),
            "catalog": self.file_state(self.catalog_path),
            "board": self.file_state(self.board_script),
        }
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "ok")
        self.assertEqual(self.fixture_listing(), listing_before)
        self.assertEqual(
            {
                "db": self.file_state(self.db_path),
                "catalog": self.file_state(self.catalog_path),
                "board": self.file_state(self.board_script),
            },
            states_before,
        )

    def test_collect_reads_a_read_only_database_without_writing(self):
        config = self.prepare_ready()
        os.chmod(str(self.db_path), 0o444)
        self.addCleanup(os.chmod, str(self.db_path), 0o644)
        frozen = rt.freeze_identity(config, expected_board_sha256=self.board_sha)
        state_before = self.file_state(self.db_path)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "ok")
        self.assertIs(payload["ready"], True)
        self.assertEqual(self.file_state(self.db_path), state_before)


# --------------------------------------------------------------------------- #
# Finite shared SQLite VM-step budget (bounds duplicate scans, fails closed)
# --------------------------------------------------------------------------- #
class SqliteStepBudgetTests(RuntimeFixtureTestCase):
    """The observation-wide VM-step budget must be installed before any SQL and shared."""

    def setUp(self):
        super().setUp()
        self.budget_before = rt.MAX_SQLITE_VM_STEPS
        self.addCleanup(setattr, rt, "MAX_SQLITE_VM_STEPS", self.budget_before)

    def set_budget(self, steps):
        rt.MAX_SQLITE_VM_STEPS = steps

    def test_module_exposes_a_finite_deterministic_step_budget(self):
        self.assertIsInstance(rt.MAX_SQLITE_VM_STEPS, int)
        self.assertGreater(rt.MAX_SQLITE_VM_STEPS, 0)
        self.assertEqual(rt.SQLITE_PROGRESS_INTERVAL, 1)
        self.assertGreater(rt.SQLITE_CONNECT_TIMEOUT_SECONDS, 0)

    def test_default_budget_keeps_a_full_schema_fixture_ready_and_counts_steps(self):
        self.write_database(
            projects=("d4-product-live",),
            extra_tasks=(("task-live-1", "d4-product-live"),),
        )
        self.write_catalog()
        observation = rt._observe_database(str(self.db_path))
        self.assertIs(observation["verified"], True)
        # Proof the progress handler really was installed on the connection and called.
        self.assertGreater(observation["vmSteps"], 0)
        payload = rt.collect_health(
            rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        )
        self.assertEqual(payload["reason"], "ok")
        self.assertIs(payload["ready"], True)

    def test_shrinking_only_the_budget_turns_the_same_fixture_fail_closed(self):
        # Same fixture, same frozen identity; only the finite budget changes. Without an
        # installed handler a one-step budget could not change the verdict at all.
        self.write_database(
            projects=("d4-product-live",),
            extra_tasks=(("task-live-1", "d4-product-live"),),
        )
        self.write_catalog()
        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        self.assertIs(rt.collect_health(frozen)["ready"], True)

        self.set_budget(1)
        payload = rt.collect_health(frozen)
        self.assertIs(payload["ready"], False)
        self.assertNotEqual(payload["reason"], "ok")
        self.assertIn(payload["reason"], ("database_invalid", "source_unreadable"))
        self.assertIn(payload["reason"], rt.COLLECT_REASONS)
        self.assertEqual(payload, d4r.validate_health_payload(payload))

    def test_duplicate_heavy_project_scan_is_bounded_by_the_shared_budget(self):
        # A ``LIMIT`` cannot bound a duplicate-heavy DISTINCT scan: SQLite may read every
        # duplicate row while returning a single distinct row, far below the row bound, so
        # only the finite shared step budget bounds the work. The same frozen identity is
        # ready with the default budget and fail-closed with the measured budget halved.
        duplicates = 5000
        self.write_catalog(document=catalog_document(project_id="d4-product"))
        self.write_database(
            projects=("d4-product-live",),
            extra_tasks=tuple(
                ("task-dup-%05d" % index, "dup-project") for index in range(duplicates)
            ),
        )
        observation = rt._observe_database(str(self.db_path))
        self.assertIs(observation["verified"], True)
        self.assertEqual(
            observation["project_ids"], frozenset({"d4-product-live", "dup-project"})
        )
        measured = observation["vmSteps"]
        # Work scales with the duplicate rows even though the distinct set stays tiny.
        self.assertGreater(measured, 4 * rt.MAX_IDENTITY_ROWS)

        connection = sqlite3.connect(str(self.db_path))
        try:
            rows = connection.execute(
                "SELECT DISTINCT project_id FROM tasks LIMIT ?",
                (rt.MAX_IDENTITY_ROWS + 1,),
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual(len(rows), 1)

        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        ready = rt.collect_health(frozen)
        self.assertEqual(ready["reason"], "ok")
        self.assertIs(ready["ready"], True)

        self.set_budget(max(1, measured // 2))
        bounded = rt._observe_database(str(self.db_path))
        # The budget aborts the scan itself (the connection stays readable) and no partial
        # project set is ever reported.
        self.assertIs(bounded["readable"], True)
        self.assertIs(bounded["verified"], False)
        self.assertEqual(bounded["project_ids"], frozenset())
        self.assertGreater(bounded["vmSteps"], 0)
        self.assertLessEqual(bounded["vmSteps"], measured)

        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "database_invalid")
        self.assertIs(payload["ready"], False)
        self.assertIs(payload["dataSource"]["present"], True)


# --------------------------------------------------------------------------- #
# Catalog material validation (bounded, read-only, confined to the catalog parent)
# --------------------------------------------------------------------------- #
class CatalogMaterialTests(RuntimeFixtureTestCase):
    def frozen_catalog_only(self):
        return rt.freeze_identity(self.config(db=False), expected_board_sha256=self.board_sha)

    def assert_no_catalog_path_leak(self, payload):
        """Every catalog regression proves the frozen payload never carries path material."""
        self.assertEqual(d4r.validate_health_payload(payload), payload)
        text = d4r.serialize_health_payload(payload).decode("utf-8")
        for forbidden in ("/", "\\", "Traceback", "\n", " "):
            self.assertNotIn(forbidden, text)
        for value in (str(self.root), str(self.catalog_path), "materials", "snapshot"):
            self.assertNotIn(value, text)

    def collect_reason(self, document):
        self.write_catalog(document=document)
        frozen = self.frozen_catalog_only()
        payload = rt.collect_health(frozen)
        self.assert_no_catalog_path_leak(payload)
        return payload

    def test_catalog_with_a_valid_material_is_ready(self):
        content = "# D4 material\n".encode("utf-8")
        self.write_material("materials/doc.md", content)
        payload = self.collect_reason(
            catalog_document(documents=[material_document("materials/doc.md", content)])
        )
        self.assertEqual(payload["reason"], "ok")
        self.assertIs(payload["ready"], True)

    def test_catalog_with_a_missing_material_is_catalog_invalid(self):
        payload = self.collect_reason(
            catalog_document(
                documents=[material_document("materials/absent.md", b"# absent\n")]
            )
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)

    def test_catalog_material_that_escapes_the_parent_is_catalog_invalid(self):
        cases = {
            "parent traversal": "../outside.md",
            "absolute path": "/etc/hosts",
            "dot segment": "./doc.md",
            "empty segment": "materials//doc.md",
        }
        for label, snapshot in cases.items():
            with self.subTest(case=label):
                payload = self.collect_reason(
                    catalog_document(
                        documents=[
                            {
                                "name": "doc.md",
                                "kind": "markdown",
                                "snapshot": snapshot,
                                "sha256": hashlib.sha256(b"# escaped\n").hexdigest(),
                            }
                        ]
                    )
                )
                self.assertEqual(payload["reason"], "catalog_invalid")
                self.assertIs(payload["ready"], False)

    def test_catalog_material_symlink_is_catalog_invalid(self):
        content = "# real\n".encode("utf-8")
        self.write_material("materials/real.md", content)
        os.symlink(
            str(self.root / "materials" / "real.md"),
            str(self.root / "materials" / "link.md"),
        )
        payload = self.collect_reason(
            catalog_document(documents=[material_document("materials/link.md", content)])
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)

    def test_catalog_material_symlinked_ancestor_escape_is_catalog_invalid(self):
        os.makedirs(str(self.root / "materials"), exist_ok=True)
        os.symlink(
            str(self.root.parent / "outside.md"),
            str(self.root / "materials" / "escape.md"),
        )
        payload = self.collect_reason(
            catalog_document(
                documents=[material_document("materials/escape.md", b"# escape\n")]
            )
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)

    def test_catalog_material_internal_directory_alias_symlink_is_catalog_invalid(self):
        # The observed false positive: ``materials/alias`` is a real symlink to
        # ``materials`` itself, so the material resolves back *inside* the catalog parent
        # and a path-resolution containment comparison accepts it (the counterexample
        # reported ready=true). Descriptor-relative traversal with O_NOFOLLOW refuses the
        # symlinked ancestor component itself.
        content = "# aliased\n".encode("utf-8")
        self.write_material("materials/real.md", content)
        os.symlink(str(self.root / "materials"), str(self.root / "materials" / "alias"))
        self.assertEqual(
            os.path.realpath(str(self.root / "materials" / "alias" / "real.md")),
            os.path.realpath(str(self.root / "materials" / "real.md")),
        )
        payload = self.collect_reason(
            catalog_document(
                documents=[material_document("materials/alias/real.md", content)]
            )
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)
        self.assertIs(payload["dataSource"]["present"], True)

    def test_catalog_material_directory_with_a_final_symlink_is_catalog_invalid(self):
        content = "# nested target\n".encode("utf-8")
        self.write_material("materials/nested/real.md", content)
        os.symlink(
            str(self.root / "materials" / "nested" / "real.md"),
            str(self.root / "materials" / "nested" / "link.md"),
        )
        payload = self.collect_reason(
            catalog_document(
                documents=[material_document("materials/nested/link.md", content)]
            )
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)

    def test_catalog_material_path_with_nul_or_control_characters_is_catalog_invalid(self):
        # The observed counterexample escaped as a raw ``ValueError: embedded null byte``
        # out of the collector. Control characters must be refused on the path string alone,
        # before any filesystem operation, and must fail closed as ``catalog_invalid``.
        digest = hashlib.sha256(b"# ctrl\n").hexdigest()
        cases = {
            "nul": "materials/\x00doc.md",
            "control char": "materials/\x01doc.md",
            "delete": "materials/\x7fdoc.md",
            "newline": "materials/\ndoc.md",
            "trailing nul": "doc.md\x00",
        }
        for label, snapshot in cases.items():
            with self.subTest(case=label):
                payload = self.collect_reason(
                    catalog_document(
                        documents=[
                            {
                                "name": "doc.md",
                                "kind": "markdown",
                                "snapshot": snapshot,
                                "sha256": digest,
                            }
                        ]
                    )
                )
                self.assertEqual(payload["reason"], "catalog_invalid")
                self.assertIs(payload["ready"], False)

    def test_catalog_material_path_with_a_lone_surrogate_is_catalog_invalid(self):
        # A lone surrogate cannot be encoded to UTF-8 and used to escape as a raw
        # ``UnicodeError`` (a ``ValueError``) from the collector; it must fail closed too.
        snapshot = "materials/\ud800doc.md"
        document = catalog_document(
            documents=[
                {
                    "name": "doc.md",
                    "kind": "markdown",
                    "snapshot": snapshot,
                    "sha256": hashlib.sha256(b"# surrogate\n").hexdigest(),
                }
            ]
        )
        self.catalog_path.write_bytes(
            json.dumps(document, ensure_ascii=True, sort_keys=True).encode("utf-8")
        )
        reloaded = json.loads(self.catalog_path.read_text(encoding="utf-8"))
        self.assertEqual(
            reloaded["projects"][0]["tasks"][0]["documents"][0]["snapshot"], snapshot
        )
        payload = rt.collect_health(self.frozen_catalog_only())
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)
        self.assert_no_catalog_path_leak(payload)

    def test_catalog_material_with_a_valid_nested_regular_path_is_ready(self):
        content = "# nested material\n".encode("utf-8")
        self.write_material("materials/nested/deep/doc.md", content)
        payload = self.collect_reason(
            catalog_document(
                documents=[material_document("materials/nested/deep/doc.md", content)]
            )
        )
        self.assertEqual(payload["reason"], "ok")
        self.assertIs(payload["ready"], True)

    def test_catalog_oversized_material_is_catalog_invalid(self):
        content = b"x" * (rt.MAX_CATALOG_MATERIAL_BYTES + 1)
        self.write_material("materials/big.md", content)
        payload = self.collect_reason(
            catalog_document(documents=[material_document("materials/big.md", content)])
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)

    def test_catalog_material_with_invalid_utf8_is_catalog_invalid(self):
        content = b"\xff\xfe not utf-8 \x80"
        self.write_material("materials/bad.md", content)
        payload = self.collect_reason(
            catalog_document(documents=[material_document("materials/bad.md", content)])
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)

    def test_catalog_material_with_invalid_declared_digest_is_catalog_invalid(self):
        payload = self.collect_reason(
            catalog_document(
                documents=[
                    {
                        "name": "doc.md",
                        "kind": "markdown",
                        "snapshot": "materials/doc.md",
                        "sha256": "NOTHEX",
                    }
                ]
            )
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)

    def test_catalog_material_digest_mismatch_is_catalog_invalid(self):
        content = "# material\n".encode("utf-8")
        self.write_material("materials/doc.md", content)
        payload = self.collect_reason(
            catalog_document(
                documents=[material_document("materials/doc.md", b"# different\n")]
            )
        )
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)

    def test_catalog_material_change_after_freeze_is_catalog_invalid(self):
        content = "# version 1\n".encode("utf-8")
        target = self.write_material("materials/doc.md", content)
        self.write_catalog(
            document=catalog_document(documents=[material_document("materials/doc.md", content)])
        )
        frozen = self.frozen_catalog_only()
        self.assertEqual(rt.collect_health(frozen)["reason"], "ok")

        catalog_state = self.file_state(self.catalog_path)
        target.write_bytes(b"# version 2\n")
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "catalog_invalid")
        self.assertIs(payload["ready"], False)
        self.assertEqual(self.file_state(self.catalog_path), catalog_state)

    def test_catalog_source_path_is_never_read(self):
        content = "# material\n".encode("utf-8")
        self.write_material("materials/doc.md", content)
        payload = self.collect_reason(
            catalog_document(
                documents=[
                    material_document(
                        "materials/doc.md", content, source_path="../outside/secret.md"
                    )
                ]
            )
        )
        self.assertEqual(payload["reason"], "ok")
        self.assertIs(payload["ready"], True)

    def test_collect_does_not_create_or_modify_catalog_materials(self):
        content = "# material\n".encode("utf-8")
        target = self.write_material("materials/doc.md", content)
        self.write_catalog(
            document=catalog_document(documents=[material_document("materials/doc.md", content)])
        )
        frozen = self.frozen_catalog_only()
        listing_before = self.fixture_listing()
        state_before = self.file_state(target)
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "ok")
        self.assertEqual(self.fixture_listing(), listing_before)
        self.assertEqual(self.file_state(target), state_before)


# --------------------------------------------------------------------------- #
# Privacy, bounded output and module surface
# --------------------------------------------------------------------------- #
class PrivacyAndBoundsTests(RuntimeFixtureTestCase):
    def test_collected_payload_carries_no_paths_sql_tokens_or_exception_text(self):
        config, frozen = self.frozen_ready()
        payload = rt.collect_health(frozen)
        text = d4r.serialize_health_payload(payload).decode("utf-8")
        for forbidden in ("/", "sqlite3", "Traceback", "token", "Error", "exception", "\n", " "):
            self.assertNotIn(forbidden, text)
        for value in (
            config["dbPath"],
            config["catalogPath"],
            config["runtimeDir"],
            config["boardScript"],
        ):
            self.assertNotIn(value, text)

    def test_collected_payload_leaves_are_bounded_values(self):
        _, frozen = self.frozen_ready()
        payload = rt.collect_health(frozen)
        literals = {
            "d4-board-health.v1",
            "d4-board",
            "sqlite",
            "catalog",
            "sqlite+catalog",
            "none",
        } | set(REASONS)
        leaves = []

        def walk(node):
            if isinstance(node, dict):
                for value in node.values():
                    walk(value)
            else:
                leaves.append(node)

        walk(payload)
        self.assertTrue(leaves)
        for leaf in leaves:
            if isinstance(leaf, bool):
                continue
            self.assertIsInstance(leaf, str)
            if len(leaf) == 64 and all(char in LOWER_HEX for char in leaf):
                continue
            self.assertIn(leaf, literals)

    def test_serialized_payload_is_canonical_bounded_and_reparsable(self):
        _, frozen = self.frozen_ready()
        payload = rt.collect_health(frozen)
        raw = d4r.serialize_health_payload(payload)
        self.assertEqual(raw, independent_canonical_bytes(payload))
        self.assertNotIn(b"\n", raw)
        self.assertLessEqual(len(raw), 1024)
        self.assertEqual(json.loads(raw.decode("utf-8")), payload)

    def test_drift_payloads_stay_inside_the_frozen_reason_enum(self):
        self.write_database()
        self.write_catalog()
        frozen = rt.freeze_identity(self.config(), expected_board_sha256=self.board_sha)
        # Rebuild the snapshot path indirectly: every reachable reason must be frozen.
        self.assertEqual(rt.COLLECT_REASONS, COLLECT_REASONS)
        self.assertTrue(set(rt.COLLECT_REASONS) <= set(d4r.REASONS))
        self.assertEqual(rt.HEALTH_REASONS, frozenset(d4r.REASONS))
        self.assertEqual(rt.HEALTH_SCHEMA, "d4-board-health.v1")
        self.assertEqual(rt.SERVICE_NAME, "d4-board")
        self.assertEqual(rt.AUTH_NONE, "none")
        self.assertEqual(rt.CONFIG_FIELDS, CONFIG_FIELD_NAMES)
        self.assertNotIn("source_changed", rt.COLLECT_REASONS)
        payload = rt.collect_health(frozen)
        self.assertIn(payload["reason"], rt.COLLECT_REASONS)

    def test_module_exposes_no_service_network_or_cli_surface(self):
        self.assertEqual(
            set(rt.__all__),
            {"FrozenIdentity", "HealthRuntimeError", "freeze_identity", "collect_health"},
        )
        for name in (
            "socket",
            "http",
            "subprocess",
            "ssl",
            "requests",
            "urllib",
            "plistlib",
            "threading",
            "asyncio",
            "select",
            "shutil",
            "tempfile",
        ):
            self.assertNotIn(name, vars(rt))
        self.assertFalse(hasattr(rt, "main"))
        for name in vars(rt):
            lowered = name.lower()
            for forbidden in (
                "server",
                "handler",
                "launchd",
                "healthz",
                "plist",
                "migrat",
                "restart",
                "endpoint",
                "install",
            ):
                self.assertNotIn(forbidden, lowered)

    def test_module_reads_sources_with_bounded_limits(self):
        self.assertIsInstance(rt.MAX_BOARD_SCRIPT_BYTES, int)
        self.assertIsInstance(rt.MAX_CATALOG_BYTES, int)
        self.assertIsInstance(rt.MAX_IDENTITY_ROWS, int)
        self.assertIsInstance(rt.MAX_SCHEMA_TABLES, int)
        self.assertGreater(rt.MAX_BOARD_SCRIPT_BYTES, 0)
        self.assertGreater(rt.MAX_CATALOG_BYTES, 0)
        self.assertIsInstance(rt.MAX_SQLITE_VM_STEPS, int)
        self.assertGreater(rt.MAX_SQLITE_VM_STEPS, 0)
        self.assertGreater(rt.SQLITE_PROGRESS_INTERVAL, 0)
        self.assertGreater(rt.SQLITE_CONNECT_TIMEOUT_SECONDS, 0)
        self.assertEqual(rt.FREEZE_ERROR_CODES[0], "invalid_config")
        self.assertIn("board_sha_unavailable", rt.FREEZE_ERROR_CODES)
        self.assertIn("invalid_frozen_identity", rt.FREEZE_ERROR_CODES)


# --------------------------------------------------------------------------- #
# Two-step contract against the frozen reader protocol
# --------------------------------------------------------------------------- #
class FrozenReaderIntegrationTests(RuntimeFixtureTestCase):
    def test_collected_payload_validates_and_evaluates_with_the_frozen_reader(self):
        _, frozen = self.frozen_ready()
        payload = rt.collect_health(frozen)
        self.assertEqual(d4r.validate_health_payload(payload), payload)
        self.assertEqual(
            d4r.evaluate_health_payload(
                payload,
                expected_board_sha256=frozen.board_sha256,
                expected_config_identity=frozen.config_identity,
                expected_data_source_identity=frozen.data_source_identity,
            ),
            (True, "ok"),
        )

    def test_replaced_source_mismatches_the_frozen_expected_identity(self):
        _, frozen = self.frozen_ready()
        replacement = self.write_database(
            self.root / "replacement.sqlite3",
            projects=("d4-product-live",),
            extra_tasks=(("task-live-1", "d4-product-live"),),
        )
        os.replace(str(replacement), str(self.db_path))
        payload = rt.collect_health(frozen)
        self.assertEqual(payload["reason"], "source_replaced")
        self.assertEqual(
            d4r.evaluate_health_payload(
                payload,
                expected_board_sha256=frozen.board_sha256,
                expected_config_identity=frozen.config_identity,
                expected_data_source_identity=frozen.data_source_identity,
            ),
            (False, "data_source_mismatch"),
        )

    def test_unready_payload_reports_its_reason_against_matching_identities(self):
        _, frozen = self.frozen_ready()
        self.board_script.write_text("changed board source\n", encoding="utf-8")
        payload = rt.collect_health(frozen)
        self.assertEqual(
            d4r.evaluate_health_payload(
                payload,
                expected_board_sha256=frozen.board_sha256,
                expected_config_identity=frozen.config_identity,
                expected_data_source_identity=frozen.data_source_identity,
            ),
            (False, "board_sha_unavailable"),
        )

    def test_runtime_reuses_the_candidate_reader_module(self):
        self.assertEqual(Path(rt._READER_PATH).name, "d4_health_reader.py")
        self.assertEqual(rt._reader.HEALTH_SCHEMA, "d4-board-health.v1")
        self.assertEqual(rt._reader.CONFIG_FIELDS, CONFIG_FIELD_NAMES)
        self.assertEqual(rt._reader.SNAPSHOT_SCHEMA, "d4-source-snapshot.v1")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
