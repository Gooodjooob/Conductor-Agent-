"""Isolated SQLite tests for the explicit planning-schema migration candidate.

These tests execute real SQLite; they never use string-only assertions. Every
temporary resource is created inside this round's workspace
(``tests/runtime``) or in memory, and is cleaned up on teardown. No real service,
Agent, daily CRM/ERP store or network is touched.
"""

import os
import sqlite3
import sys
import unittest
import unittest.mock as mock
import uuid

_WORKSPACE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _WORKSPACE_ROOT not in sys.path:
    sys.path.insert(0, _WORKSPACE_ROOT)

from storage import planning_schema  # noqa: E402

_RUNTIME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runtime")

_EXECUTION_TABLES = ("tasks", "attempts", "events", "deliveries", "reviews")

_FROZEN_TABLES = (
    "planning_schema_meta",
    "planning_sources",
    "planning_snapshots",
    "planning_audit",
)

_FROZEN_TRIGGERS = (
    "planning_snapshots_reject_update",
    "planning_snapshots_reject_delete",
    "planning_audit_reject_update",
    "planning_audit_reject_delete",
)


class _SchemaTestBase(unittest.TestCase):
    """Helpers: synthetic databases, real-SQLite inserts and cleanup."""

    def setUp(self):
        self._connections = []
        self._paths = []

    def tearDown(self):
        for connection in self._connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._connections = []
        for path in self._paths:
            for candidate in (path, path + "-wal", path + "-shm", path + "-journal"):
                try:
                    os.remove(candidate)
                except OSError:
                    pass
        self._paths = []
        # Temporary SQLite resources must stay inside this round's workspace and
        # must not survive the test.
        if os.path.isdir(_RUNTIME_DIR):
            leftovers = [
                name
                for name in os.listdir(_RUNTIME_DIR)
                if name.startswith("planning_schema_")
            ]
            if leftovers:
                raise AssertionError(
                    "temporary SQLite files were not cleaned up: %r" % leftovers
                )

    # -- connection helpers -------------------------------------------------

    def connect(self, path=None):
        connection = sqlite3.connect(path or ":memory:")
        self._connections.append(connection)
        return connection

    def connect_file(self):
        os.makedirs(_RUNTIME_DIR, exist_ok=True)
        path = os.path.join(
            _RUNTIME_DIR, "planning_schema_%s.sqlite3" % uuid.uuid4().hex
        )
        self._paths.append(path)
        return self.connect(path), path

    def synthetic_database(self, with_execution_tables=True):
        """Base store fixture: workspace_projects + execution fact sentinels."""
        connection = self.connect()
        connection.execute(
            "CREATE TABLE workspace_projects (project_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workspace_projects (project_id, name) VALUES ('proj-a', 'Project A')"
        )
        connection.execute(
            "INSERT INTO workspace_projects (project_id, name) VALUES ('proj-b', 'Project B')"
        )
        if with_execution_tables:
            for table in _EXECUTION_TABLES:
                connection.execute(
                    "CREATE TABLE %s (id TEXT PRIMARY KEY, note TEXT NOT NULL)" % table
                )
                connection.execute(
                    "INSERT INTO %s (id, note) VALUES ('sentinel-1', 'sentinel')" % table
                )
        connection.commit()
        return connection

    def migrated_database(self):
        connection = self.synthetic_database()
        result = planning_schema.migrate_planning_schema(connection)
        self.assertEqual(result["status"], "applied")
        return connection

    # -- observation helpers ------------------------------------------------

    def schema_snapshot(self, connection):
        rows = connection.execute(
            "SELECT type, name, COALESCE(sql, '') FROM sqlite_master ORDER BY type, name"
        ).fetchall()
        return [tuple(row) for row in rows]

    def object_names(self, connection, kind=None):
        if kind is None:
            rows = connection.execute("SELECT name FROM sqlite_master").fetchall()
        else:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = ?", (kind,)
            ).fetchall()
        return sorted(row[0] for row in rows)

    def execution_row_counts(self, connection):
        return {
            table: connection.execute(
                "SELECT COUNT(*) FROM %s" % table
            ).fetchone()[0]
            for table in _EXECUTION_TABLES
        }

    # -- real-SQLite insert helpers ----------------------------------------

    def insert_source(
        self,
        connection,
        project="proj-a",
        source="src-1",
        kind="product",
        path="/planning/product.md",
        commit=True,
    ):
        connection.execute(
            "INSERT INTO planning_sources (project_id, planning_source_id, source_kind,"
            " source_path, allowed_root, registered_by_json, registered_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                project,
                source,
                kind,
                path,
                "/planning",
                '{"actor": "dev-ckb1"}',
                "2026-10-10T20:00:00+08:00",
            ),
        )
        if commit:
            connection.commit()

    def insert_snapshot(
        self,
        connection,
        snapshot_id="snap-1",
        project="proj-a",
        source="src-1",
        revision=1,
        source_bytes=128,
        status=None,
        commit=True,
    ):
        columns = (
            "snapshot_id, project_id, planning_source_id, snapshot_revision,"
            " document_version, source_sha256, source_bytes, canonical_digest,"
            " payload_json, imported_by_json, imported_at"
        )
        params = [
            snapshot_id,
            project,
            source,
            revision,
            "doc-v1",
            "a" * 64,
            source_bytes,
            "b" * 64,
            "{}",
            '{"actor": "dev-ckb1"}',
            "2026-10-10T20:05:00+08:00",
        ]
        if status is not None:
            columns += ", status"
            params.append(status)
        placeholders = ", ".join("?" for _ in params)
        connection.execute(
            "INSERT INTO planning_snapshots (%s) VALUES (%s)" % (columns, placeholders),
            tuple(params),
        )
        if commit:
            connection.commit()

    def insert_audit(
        self,
        connection,
        audit_id="audit-1",
        project="proj-a",
        source="src-1",
        action="register_source",
        commit=True,
    ):
        connection.execute(
            "INSERT INTO planning_audit (audit_id, project_id, planning_source_id, action,"
            " actor_json, reason, occurred_at, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_id,
                project,
                source,
                action,
                '{"actor": "dev-ckb1"}',
                "frozen schema test",
                "2026-10-10T20:06:00+08:00",
                "{}",
            ),
        )
        if commit:
            connection.commit()

    def assert_integrity_error(self, connection, sql, params=()):
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(sql, params)
        connection.rollback()


class PlanningSchemaMigrationTests(_SchemaTestBase):
    """Explicit migration: first run, no-op, fail-closed, atomicity."""

    def test_requires_caller_supplied_sqlite_connection(self):
        with self.assertRaises(planning_schema.PlanningSchemaError):
            planning_schema.migrate_planning_schema(":memory:")
        with self.assertRaises(planning_schema.PlanningSchemaError):
            planning_schema.migrate_planning_schema(None)

    def test_import_creates_nothing_on_a_bare_connection(self):
        connection = self.connect()
        self.assertEqual(self.object_names(connection), [])
        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 0)

    def test_first_migration_creates_frozen_schema(self):
        connection = self.synthetic_database()

        result = planning_schema.migrate_planning_schema(connection)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(result["version"], 1)
        self.assertEqual(
            result["created"], sorted(list(_FROZEN_TABLES) + list(_FROZEN_TRIGGERS))
        )
        self.assertTrue(result["foreign_keys"])

        tables = set(self.object_names(connection, "table"))
        for table in _FROZEN_TABLES:
            self.assertIn(table, tables)

        triggers = set(self.object_names(connection, "trigger"))
        self.assertEqual(triggers, set(_FROZEN_TRIGGERS))

        markers = connection.execute(
            "SELECT singleton, version FROM planning_schema_meta"
        ).fetchall()
        self.assertEqual(markers, [(1, 1)])

        self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        self.assertFalse(connection.in_transaction)

    def test_repeat_migration_is_no_op_and_schema_unchanged(self):
        connection = self.synthetic_database()
        planning_schema.migrate_planning_schema(connection)
        before = self.schema_snapshot(connection)

        result = planning_schema.migrate_planning_schema(connection)

        self.assertEqual(result["status"], "no_op")
        self.assertEqual(result["created"], [])
        self.assertEqual(self.schema_snapshot(connection), before)
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_schema_meta").fetchone()[0], 1
        )

    def test_missing_workspace_projects_is_rejected_without_writes(self):
        connection = self.connect()
        connection.execute("CREATE TABLE unrelated (id TEXT PRIMARY KEY)")
        connection.commit()
        before = self.schema_snapshot(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        self.assertIn("workspace_projects", str(ctx.exception))
        self.assertEqual(self.schema_snapshot(connection), before)

    def test_workspace_projects_without_project_id_pk_is_rejected(self):
        connection = self.connect()
        connection.execute("CREATE TABLE workspace_projects (project_id TEXT, name TEXT)")
        connection.commit()
        before = self.schema_snapshot(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        self.assertIn("PRIMARY KEY", str(ctx.exception))
        self.assertEqual(self.schema_snapshot(connection), before)

    def test_partial_schema_is_rejected_without_writes(self):
        connection = self.synthetic_database()
        connection.execute(
            "CREATE TABLE planning_sources (project_id TEXT NOT NULL,"
            " planning_source_id TEXT NOT NULL, PRIMARY KEY (project_id, planning_source_id))"
        )
        connection.commit()
        before = self.schema_snapshot(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        self.assertIn("partial", str(ctx.exception).lower())
        self.assertEqual(self.schema_snapshot(connection), before)
        self.assertNotIn("planning_snapshots", self.object_names(connection))

    def test_partial_schema_without_triggers_is_rejected_without_writes(self):
        connection = self.synthetic_database()
        # All four tables plus the version marker, but no append-only triggers.
        for statement in planning_schema._MIGRATION_STATEMENTS[:4]:
            connection.execute(statement)
        connection.execute(planning_schema._MARKER_INSERT)
        connection.commit()
        before = self.schema_snapshot(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        self.assertIn("partial", str(ctx.exception).lower())
        self.assertEqual(self.schema_snapshot(connection), before)
        self.assertEqual(self.object_names(connection, "trigger"), [])

    def test_wrong_named_table_schema_is_rejected_without_writes(self):
        connection = self.synthetic_database()
        connection.execute("CREATE TABLE planning_schema_meta (note TEXT NOT NULL)")
        connection.execute("INSERT INTO planning_schema_meta (note) VALUES ('legacy')")
        connection.commit()
        before = self.schema_snapshot(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        self.assertIn("planning_schema_meta", str(ctx.exception))
        self.assertEqual(self.schema_snapshot(connection), before)

    def test_unknown_version_is_rejected_without_writes(self):
        connection = self.synthetic_database()
        connection.execute(
            "CREATE TABLE planning_schema_meta (singleton INTEGER PRIMARY KEY,"
            " version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO planning_schema_meta (singleton, version) VALUES (1, 7)"
        )
        connection.commit()
        before = self.schema_snapshot(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        self.assertIn("7", str(ctx.exception))
        self.assertEqual(self.schema_snapshot(connection), before)
        self.assertNotIn("planning_sources", self.object_names(connection))

    def test_conflicting_frozen_tables_are_rejected_without_writes(self):
        connection = self.synthetic_database()
        connection.execute(
            "CREATE TABLE planning_schema_meta (singleton INTEGER PRIMARY KEY,"
            " version INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO planning_schema_meta (singleton, version) VALUES (1, 1)"
        )
        for name in ("planning_sources", "planning_snapshots", "planning_audit"):
            connection.execute("CREATE TABLE %s (id TEXT PRIMARY KEY)" % name)
        # Same object names and types, but a different definition for every one
        # of them: the migration must refuse instead of repairing or overwriting.
        for name, (kind, _) in planning_schema._EXPECTED_OBJECTS.items():
            if kind == "trigger":
                connection.execute(
                    "CREATE TRIGGER %s BEFORE UPDATE ON planning_audit"
                    " BEGIN SELECT 1; END" % name
                )
        connection.commit()
        before = self.schema_snapshot(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        self.assertIn("conflict", str(ctx.exception).lower())
        self.assertEqual(self.schema_snapshot(connection), before)

    def test_open_user_transaction_is_rejected_and_not_committed(self):
        connection = self.synthetic_database()
        connection.execute(
            "INSERT INTO workspace_projects (project_id, name) VALUES ('proj-c', 'Project C')"
        )
        self.assertTrue(connection.in_transaction)
        before = self.schema_snapshot(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        self.assertIn("transaction", str(ctx.exception).lower())
        # The caller's transaction is still open: we did not commit it.
        self.assertTrue(connection.in_transaction)
        self.assertEqual(self.schema_snapshot(connection), before)
        self.assertNotIn("planning_schema_meta", self.object_names(connection))

        connection.rollback()
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM workspace_projects").fetchone()[0], 2
        )

    def test_injected_ddl_failure_rolls_back_and_preserves_sentinels(self):
        connection = self.synthetic_database()
        before_schema = self.schema_snapshot(connection)
        before_counts = self.execution_row_counts(connection)
        before_projects = connection.execute(
            "SELECT project_id, name FROM workspace_projects ORDER BY project_id"
        ).fetchall()
        before_tasks = connection.execute(
            "SELECT id, note FROM tasks ORDER BY id"
        ).fetchall()

        # A genuine DDL failure injected after the marker insert: the duplicate
        # table name collides with a table created earlier in the same transaction.
        broken = tuple(planning_schema._MIGRATION_STATEMENTS) + (
            "CREATE TABLE planning_audit (audit_id TEXT PRIMARY KEY NOT NULL)",
        )
        with mock.patch.object(planning_schema, "_MIGRATION_STATEMENTS", broken):
            with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
                planning_schema.migrate_planning_schema(connection)

        self.assertIn("rolled back", str(ctx.exception))
        self.assertFalse(connection.in_transaction)
        self.assertEqual(self.schema_snapshot(connection), before_schema)
        self.assertEqual(self.execution_row_counts(connection), before_counts)
        self.assertEqual(
            connection.execute(
                "SELECT project_id, name FROM workspace_projects ORDER BY project_id"
            ).fetchall(),
            before_projects,
        )
        self.assertEqual(
            connection.execute("SELECT id, note FROM tasks ORDER BY id").fetchall(),
            before_tasks,
        )
        self.assertFalse(
            any(name.startswith("planning_") for name in self.object_names(connection))
        )

    def test_recovers_after_a_rolled_back_attempt(self):
        connection = self.synthetic_database()
        broken = tuple(planning_schema._MIGRATION_STATEMENTS) + (
            "CREATE TABLE planning_audit (audit_id TEXT PRIMARY KEY NOT NULL)",
        )
        with mock.patch.object(planning_schema, "_MIGRATION_STATEMENTS", broken):
            with self.assertRaises(planning_schema.PlanningSchemaError):
                planning_schema.migrate_planning_schema(connection)

        result = planning_schema.migrate_planning_schema(connection)

        self.assertEqual(result["status"], "applied")
        self.assertEqual(
            set(self.object_names(connection, "table")) & set(_FROZEN_TABLES),
            set(_FROZEN_TABLES),
        )

    def test_close_and_reopen_reads_back_schema_and_rows(self):
        connection, path = self.connect_file()
        connection.execute(
            "CREATE TABLE workspace_projects (project_id TEXT PRIMARY KEY, name TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO workspace_projects (project_id, name) VALUES ('proj-a', 'Project A')"
        )
        connection.commit()
        planning_schema.migrate_planning_schema(connection)
        self.insert_source(connection, project="proj-a", source="src-1")
        self.insert_snapshot(connection, snapshot_id="snap-1")
        connection.close()

        reopened = self.connect(path)
        tables = set(self.object_names(reopened, "table"))
        for table in _FROZEN_TABLES:
            self.assertIn(table, tables)
        self.assertEqual(
            reopened.execute(
                "SELECT snapshot_id, status FROM planning_snapshots"
            ).fetchall(),
            [("snap-1", "candidate")],
        )
        self.assertEqual(
            planning_schema.migrate_planning_schema(reopened)["status"], "no_op"
        )

    def test_migration_writes_no_execution_facts(self):
        connection = self.synthetic_database()
        before_schema = self.schema_snapshot(connection)
        before_counts = self.execution_row_counts(connection)

        planning_schema.migrate_planning_schema(connection)

        self.assertEqual(self.execution_row_counts(connection), before_counts)
        self.assertEqual(before_counts, {table: 1 for table in _EXECUTION_TABLES})
        execution_schema = [
            row for row in self.schema_snapshot(connection)
            if row[1] in set(_EXECUTION_TABLES) | {"workspace_projects"}
        ]
        expected_schema = [
            row for row in before_schema
            if row[1] in set(_EXECUTION_TABLES) | {"workspace_projects"}
        ]
        self.assertEqual(execution_schema, expected_schema)

    def test_no_user_version_or_alter_of_existing_tables(self):
        connection = self.synthetic_database()
        before = self.schema_snapshot(connection)
        planning_schema.migrate_planning_schema(connection)
        after = self.schema_snapshot(connection)

        before_map = {name: sql for _, name, sql in before}
        after_map = {name: sql for _, name, sql in after}
        for name, sql in before_map.items():
            self.assertEqual(after_map[name], sql)
        self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_meta_marker_rejects_other_versions_and_singletons(self):
        connection = self.migrated_database()

        self.assert_integrity_error(
            connection,
            "INSERT INTO planning_schema_meta (singleton, version) VALUES (2, 1)",
        )
        self.assert_integrity_error(
            connection,
            "INSERT INTO planning_schema_meta (singleton, version) VALUES (1, 1)",
        )
        self.assert_integrity_error(
            connection, "UPDATE planning_schema_meta SET version = 2"
        )
        self.assertEqual(
            connection.execute(
                "SELECT singleton, version FROM planning_schema_meta"
            ).fetchall(),
            [(1, 1)],
        )


class PlanningSchemaConstraintTests(_SchemaTestBase):
    """Frozen constraints must be enforced by real SQLite."""

    def test_source_kind_enum_is_enforced(self):
        connection = self.migrated_database()

        self.assert_integrity_error(
            connection,
            "INSERT INTO planning_sources (project_id, planning_source_id, source_kind,"
            " source_path, allowed_root, registered_by_json, registered_at)"
            " VALUES ('proj-a', 'src-x', 'unregistered', '/planning/x.md', '/planning',"
            " '{}', '2026-10-10T20:00:00+08:00')",
        )
        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_sources").fetchone()[0], 0
        )

    def test_foreign_key_rejects_unregistered_project(self):
        connection = self.migrated_database()

        self.assert_integrity_error(
            connection,
            "INSERT INTO planning_sources (project_id, planning_source_id, source_kind,"
            " source_path, allowed_root, registered_by_json, registered_at)"
            " VALUES ('proj-missing', 'src-1', 'product', '/planning/p.md', '/planning',"
            " '{}', '2026-10-10T20:00:00+08:00')",
        )

    def test_foreign_key_rejects_cross_project_snapshot(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-a")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_snapshot(
                connection, snapshot_id="snap-a", project="proj-b", source="src-a", commit=False
            )
        connection.rollback()

        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0], 0
        )

    def test_foreign_key_rejects_audit_for_unknown_source(self):
        connection = self.migrated_database()

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_audit(connection, source="src-missing", commit=False)
        connection.rollback()

        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_audit").fetchone()[0], 0
        )

    def test_source_path_unique_within_project_and_allowed_across_projects(self):
        connection = self.migrated_database()
        self.insert_source(
            connection, project="proj-a", source="src-1", path="/planning/shared.md"
        )

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_source(
                connection,
                project="proj-a",
                source="src-2",
                path="/planning/shared.md",
                commit=False,
            )
        connection.rollback()

        self.insert_source(
            connection, project="proj-b", source="src-3", path="/planning/shared.md"
        )
        rows = connection.execute(
            "SELECT project_id, planning_source_id FROM planning_sources ORDER BY project_id"
        ).fetchall()
        self.assertEqual(rows, [("proj-a", "src-1"), ("proj-b", "src-3")])

    def test_snapshot_revision_composite_uniqueness(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-1")
        self.insert_snapshot(connection, snapshot_id="snap-1", revision=1)

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_snapshot(connection, snapshot_id="snap-2", revision=1, commit=False)
        connection.rollback()

        self.insert_snapshot(connection, snapshot_id="snap-2", revision=2)
        rows = connection.execute(
            "SELECT snapshot_id, snapshot_revision FROM planning_snapshots"
            " ORDER BY snapshot_revision"
        ).fetchall()
        self.assertEqual(rows, [("snap-1", 1), ("snap-2", 2)])

    def test_snapshot_id_is_globally_unique(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-1")
        self.insert_source(connection, project="proj-b", source="src-1")
        self.insert_snapshot(connection, snapshot_id="snap-1", project="proj-a", source="src-1")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_snapshot(
                connection,
                snapshot_id="snap-1",
                project="proj-b",
                source="src-1",
                revision=5,
                commit=False,
            )
        connection.rollback()

        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0], 1
        )

    def test_invalid_snapshot_revision_is_rejected(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-1")

        for revision in (0, -1, "abc", 1.5):
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_snapshot(
                    connection,
                    snapshot_id="snap-%s" % revision,
                    revision=revision,
                    commit=False,
                )
            connection.rollback()

        self.assertEqual(
            connection.execute("SELECT COUNT(*) FROM planning_snapshots").fetchone()[0], 0
        )

    def test_invalid_source_bytes_is_rejected(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-1")

        for size in (-1, "abc"):
            with self.assertRaises(sqlite3.IntegrityError):
                self.insert_snapshot(
                    connection,
                    snapshot_id="snap-bytes-%s" % size,
                    source_bytes=size,
                    commit=False,
                )
            connection.rollback()

        self.insert_snapshot(connection, snapshot_id="snap-zero", source_bytes=0)
        self.assertEqual(
            connection.execute(
                "SELECT source_bytes FROM planning_snapshots"
            ).fetchall(),
            [(0,)],
        )

    def test_snapshot_status_is_frozen_to_candidate(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-1")

        with self.assertRaises(sqlite3.IntegrityError):
            self.insert_snapshot(
                connection, snapshot_id="snap-active", status="active", commit=False
            )
        connection.rollback()

        self.insert_snapshot(connection, snapshot_id="snap-ok")
        self.assertEqual(
            connection.execute("SELECT status FROM planning_snapshots").fetchall(),
            [("candidate",)],
        )

    def test_snapshot_update_and_delete_are_rejected(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-1")
        self.insert_snapshot(connection, snapshot_id="snap-1")

        self.assert_integrity_error(
            connection, "UPDATE planning_snapshots SET document_version = 'doc-v2'"
        )
        self.assert_integrity_error(connection, "DELETE FROM planning_snapshots")

        self.assertEqual(
            connection.execute(
                "SELECT snapshot_id, document_version FROM planning_snapshots"
            ).fetchall(),
            [("snap-1", "doc-v1")],
        )

    def test_audit_update_and_delete_are_rejected(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-1")
        self.insert_audit(connection, audit_id="audit-1")

        self.assert_integrity_error(
            connection, "UPDATE planning_audit SET action = 'tampered'"
        )
        self.assert_integrity_error(connection, "DELETE FROM planning_audit")

        self.assertEqual(
            connection.execute("SELECT audit_id, action FROM planning_audit").fetchall(),
            [("audit-1", "register_source")],
        )

    def test_new_snapshot_revision_can_be_appended_but_not_edited(self):
        connection = self.migrated_database()
        self.insert_source(connection, project="proj-a", source="src-1")
        self.insert_snapshot(connection, snapshot_id="snap-1", revision=1)
        self.insert_snapshot(connection, snapshot_id="snap-2", revision=2)

        rows = connection.execute(
            "SELECT snapshot_id, snapshot_revision FROM planning_snapshots"
            " ORDER BY snapshot_revision"
        ).fetchall()
        self.assertEqual(rows, [("snap-1", 1), ("snap-2", 2)])
        self.assert_integrity_error(
            connection, "UPDATE planning_snapshots SET snapshot_revision = 9"
        )


# ---------------------------------------------------------------------------
# 017 regression: the SQL comparison must not erase literal differences
# ---------------------------------------------------------------------------


def _frozen_statements_with_changed_literal(old, new, occurrences):
    """Copy the frozen migration statements, changing one literal in one of them.

    Raises unless ``old`` appears in exactly one frozen statement, exactly
    ``occurrences`` times there. That keeps every regression below an isolated
    single-literal difference instead of a broad textual rewrite, so a failure
    cannot be blamed on any other schema change.
    """
    changed = 0
    statements = []
    for statement in planning_schema._MIGRATION_STATEMENTS:
        if old not in statement:
            statements.append(statement)
            continue
        found = statement.count(old)
        if found != occurrences:
            raise AssertionError(
                "fixture error: expected %d occurrences of %r in one frozen"
                " statement, found %d" % (occurrences, old, found)
            )
        statements.append(statement.replace(old, new))
        changed += 1
    if changed != 1:
        raise AssertionError(
            "fixture error: expected exactly one frozen statement to change,"
            " changed %d" % changed
        )
    return tuple(statements)


class PlanningSchemaLiteralComparisonTests(_SchemaTestBase):
    """017: literal case and internal whitespace must be compared verbatim.

    The 016 candidate compared DDL with a whole-statement lower + whitespace
    collapse, so a real schema that differed from the frozen DDL only inside a
    string literal (``'candidate'`` vs ``'CANDIDATE'``, or extra spacing inside
    a trigger message) was accepted as ``no_op`` and the same-schema hard-reject
    gate never fired.

    Every test below builds a real SQLite schema that differs from the frozen
    DDL in exactly one literal, keeps pre-existing rows, and asserts that the
    migration fails closed while the schema and those rows stay untouched.
    """

    _CANDIDATE_LITERAL = ("'candidate'", "'CANDIDATE'", 2)
    _SOURCE_KIND_ENUM = (
        "('product', 'development-plan', 'other-registered')",
        "('PRODUCT', 'DEVELOPMENT-PLAN', 'OTHER-REGISTERED')",
        1,
    )
    _TRIGGER_MESSAGE_SPACING = (
        "planning_snapshots is append-only: UPDATE rejected",
        "planning_snapshots is append-only:  UPDATE rejected",
        1,
    )

    # -- fixtures and observation helpers ----------------------------------

    def real_schema_with_changed_literal(self, change):
        """Create the frozen schema in real SQLite with one literal altered."""
        old, new, occurrences = change
        connection = self.synthetic_database()
        for statement in _frozen_statements_with_changed_literal(
            old, new, occurrences
        ):
            connection.execute(statement)
        connection.commit()
        return connection

    def stored_sql(self, connection, name):
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = ?", (name,)
        ).fetchone()
        self.assertIsNotNone(row, "sqlite_master has no entry for %r" % name)
        return row[0]

    def saved_state(self, connection):
        """Everything that must survive a rejected or a repeated migration."""
        return {
            "schema": self.schema_snapshot(connection),
            "objects": self.object_names(connection),
            "marker": connection.execute(
                "SELECT singleton, version FROM planning_schema_meta"
            ).fetchall(),
            "sources": connection.execute(
                "SELECT project_id, planning_source_id, source_kind, source_path,"
                " allowed_root, registered_by_json, registered_at"
                " FROM planning_sources ORDER BY project_id, planning_source_id"
            ).fetchall(),
            "snapshots": connection.execute(
                "SELECT * FROM planning_snapshots ORDER BY snapshot_id"
            ).fetchall(),
            "audit": connection.execute(
                "SELECT * FROM planning_audit ORDER BY audit_id"
            ).fetchall(),
            "execution_rows": self.execution_row_counts(connection),
        }

    def assert_migration_rejected_without_writes(self, connection):
        before = self.saved_state(connection)

        with self.assertRaises(planning_schema.PlanningSchemaError) as ctx:
            planning_schema.migrate_planning_schema(connection)

        message = str(ctx.exception).lower()
        self.assertIn("conflict", message)
        self.assertIn("refusing", message)
        self.assertFalse(connection.in_transaction)

        after = self.saved_state(connection)
        self.assertEqual(after["schema"], before["schema"])
        self.assertEqual(after["objects"], before["objects"])
        self.assertEqual(after["marker"], before["marker"])
        self.assertEqual(after["sources"], before["sources"])
        self.assertEqual(after["snapshots"], before["snapshots"])
        self.assertEqual(after["audit"], before["audit"])
        self.assertEqual(after["execution_rows"], before["execution_rows"])
        self.assertEqual(after["execution_rows"], {t: 1 for t in _EXECUTION_TABLES})

    # -- regressions -------------------------------------------------------

    def test_snapshot_status_literal_case_change_is_rejected(self):
        connection = self.real_schema_with_changed_literal(self._CANDIDATE_LITERAL)
        # Real SQLite really keeps the altered literal, otherwise the fixture
        # itself would already have erased the difference.
        self.assertIn("'CANDIDATE'", self.stored_sql(connection, "planning_snapshots"))
        self.insert_source(connection, project="proj-a", source="src-1", kind="product")
        self.insert_snapshot(connection, snapshot_id="snap-1")
        self.insert_audit(connection, audit_id="audit-1")

        self.assert_migration_rejected_without_writes(connection)

    def test_source_kind_enum_case_change_is_rejected(self):
        connection = self.real_schema_with_changed_literal(self._SOURCE_KIND_ENUM)
        self.assertIn("'PRODUCT'", self.stored_sql(connection, "planning_sources"))
        self.insert_source(connection, project="proj-a", source="src-1", kind="PRODUCT")
        self.insert_snapshot(connection, snapshot_id="snap-1")
        self.insert_audit(connection, audit_id="audit-1")

        self.assert_migration_rejected_without_writes(connection)

    def test_trigger_literal_internal_whitespace_change_is_rejected(self):
        connection = self.real_schema_with_changed_literal(self._TRIGGER_MESSAGE_SPACING)
        self.assertIn(
            "append-only:  UPDATE rejected",
            self.stored_sql(connection, "planning_snapshots_reject_update"),
        )
        self.insert_source(connection, project="proj-a", source="src-1", kind="product")
        self.insert_snapshot(connection, snapshot_id="snap-1")
        self.insert_audit(connection, audit_id="audit-1")

        self.assert_migration_rejected_without_writes(connection)

    def test_repeat_normal_migration_is_still_no_op_with_existing_rows(self):
        connection = self.synthetic_database()
        self.assertEqual(
            planning_schema.migrate_planning_schema(connection)["status"], "applied"
        )
        self.insert_source(connection, project="proj-a", source="src-1", kind="product")
        self.insert_snapshot(connection, snapshot_id="snap-1")
        self.insert_audit(connection, audit_id="audit-1")
        before = self.saved_state(connection)

        result = planning_schema.migrate_planning_schema(connection)

        self.assertEqual(result["status"], "no_op")
        self.assertEqual(result["created"], [])
        self.assertEqual(self.saved_state(connection), before)


if __name__ == "__main__":
    unittest.main()
