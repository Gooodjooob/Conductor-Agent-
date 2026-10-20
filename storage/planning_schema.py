"""Explicit, isolated SQLite planning-schema migration candidate.

Frozen scope: ``inputs/cox-schema-decision.md`` (content state: ``planned``).

Boundaries enforced by this module:

* The only public entry point is :func:`migrate_planning_schema`. It requires a
  caller-supplied ``sqlite3.Connection`` and never opens a database path itself.
* Importing this module has no side effects: no connection, no DDL, no file
  access, no network, no dependency installation.
* Only the four frozen planning tables plus their append-only triggers are
  created. ``workspace_projects`` must already exist and is never created,
  altered or dropped.
* The migration is explicit, runs in exactly one transaction and rolls back on
  any failure. It never calls ``executescript`` (which would implicitly commit),
  never writes ``PRAGMA user_version`` and never drops an existing object.
* It never imports planning documents, never activates anything and never writes
  execution facts: no writes to tasks / attempts / events / deliveries / reviews.
* Repeating the migration is a no-op only when the real schema matches the
  frozen schema; partial, conflicting or unknown-version schemas fail closed and
  are never repaired or overwritten.
"""

import sqlite3

__all__ = [
    "MIGRATION_VERSION",
    "BASE_PROJECT_TABLE",
    "PlanningSchemaError",
    "migrate_planning_schema",
]

#: Frozen migration version stored in ``planning_schema_meta.version``.
#: It only marks this migration's own schema version; it does not mean the
#: product, the A-phase or the MVP passed anything.
MIGRATION_VERSION = 1

#: Pre-existing base table owned by the store layer. Never created here.
BASE_PROJECT_TABLE = "workspace_projects"


class PlanningSchemaError(Exception):
    """Raised when the planning schema migration must fail closed."""


# ---------------------------------------------------------------------------
# Frozen DDL (only what inputs/cox-schema-decision.md freezes)
# ---------------------------------------------------------------------------

_DDL_META = """CREATE TABLE planning_schema_meta (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    version INTEGER NOT NULL CHECK (version = 1)
)"""

_DDL_SOURCES = """CREATE TABLE planning_sources (
    project_id TEXT NOT NULL,
    planning_source_id TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('product', 'development-plan', 'other-registered')),
    source_path TEXT NOT NULL,
    allowed_root TEXT NOT NULL,
    registered_by_json TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    PRIMARY KEY (project_id, planning_source_id),
    UNIQUE (project_id, source_path),
    FOREIGN KEY (project_id) REFERENCES workspace_projects (project_id)
)"""

_DDL_SNAPSHOTS = """CREATE TABLE planning_snapshots (
    snapshot_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL,
    planning_source_id TEXT NOT NULL,
    snapshot_revision INTEGER NOT NULL CHECK (typeof(snapshot_revision) = 'integer' AND snapshot_revision > 0),
    document_version TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    source_bytes INTEGER NOT NULL CHECK (typeof(source_bytes) = 'integer' AND source_bytes >= 0),
    canonical_digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    imported_by_json TEXT NOT NULL,
    imported_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate' CHECK (status = 'candidate'),
    UNIQUE (project_id, planning_source_id, snapshot_revision),
    FOREIGN KEY (project_id, planning_source_id) REFERENCES planning_sources (project_id, planning_source_id)
)"""

_DDL_AUDIT = """CREATE TABLE planning_audit (
    audit_id TEXT PRIMARY KEY NOT NULL,
    project_id TEXT NOT NULL,
    planning_source_id TEXT NOT NULL,
    action TEXT NOT NULL,
    actor_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    FOREIGN KEY (project_id, planning_source_id) REFERENCES planning_sources (project_id, planning_source_id)
)"""

_TRIGGER_SNAPSHOTS_UPDATE = """CREATE TRIGGER planning_snapshots_reject_update
BEFORE UPDATE ON planning_snapshots
BEGIN
    SELECT RAISE(ABORT, 'planning_snapshots is append-only: UPDATE rejected');
END"""

_TRIGGER_SNAPSHOTS_DELETE = """CREATE TRIGGER planning_snapshots_reject_delete
BEFORE DELETE ON planning_snapshots
BEGIN
    SELECT RAISE(ABORT, 'planning_snapshots is append-only: DELETE rejected');
END"""

_TRIGGER_AUDIT_UPDATE = """CREATE TRIGGER planning_audit_reject_update
BEFORE UPDATE ON planning_audit
BEGIN
    SELECT RAISE(ABORT, 'planning_audit is append-only: UPDATE rejected');
END"""

_TRIGGER_AUDIT_DELETE = """CREATE TRIGGER planning_audit_reject_delete
BEFORE DELETE ON planning_audit
BEGIN
    SELECT RAISE(ABORT, 'planning_audit is append-only: DELETE rejected');
END"""

_MARKER_INSERT = (
    "INSERT INTO planning_schema_meta (singleton, version) VALUES (1, %d)"
    % MIGRATION_VERSION
)

#: Executed one statement at a time (never via ``executescript``) inside a single
#: explicit transaction. Tests may replace this tuple to inject a DDL failure.
_MIGRATION_STATEMENTS = (
    _DDL_META,
    _DDL_SOURCES,
    _DDL_SNAPSHOTS,
    _DDL_AUDIT,
    _TRIGGER_SNAPSHOTS_UPDATE,
    _TRIGGER_SNAPSHOTS_DELETE,
    _TRIGGER_AUDIT_UPDATE,
    _TRIGGER_AUDIT_DELETE,
    _MARKER_INSERT,
)

_EXPECTED_OBJECTS = {
    "planning_schema_meta": ("table", _DDL_META),
    "planning_sources": ("table", _DDL_SOURCES),
    "planning_snapshots": ("table", _DDL_SNAPSHOTS),
    "planning_audit": ("table", _DDL_AUDIT),
    "planning_snapshots_reject_update": ("trigger", _TRIGGER_SNAPSHOTS_UPDATE),
    "planning_snapshots_reject_delete": ("trigger", _TRIGGER_SNAPSHOTS_DELETE),
    "planning_audit_reject_update": ("trigger", _TRIGGER_AUDIT_UPDATE),
    "planning_audit_reject_delete": ("trigger", _TRIGGER_AUDIT_DELETE),
}

_EXPECTED_TABLE_NAMES = tuple(
    name for name, (kind, _) in _EXPECTED_OBJECTS.items() if kind == "table"
)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _normalize_sql(sql):
    """Return the verbatim comparison key for one SQL object definition.

    017 rework: this used to ``lower()`` the whole statement and collapse every
    whitespace run (``" ".join(sql.split())``). That erased differences *inside*
    SQL string literals, so a real schema whose literal case (``'candidate'``
    vs ``'CANDIDATE'``) or internal spacing differed from the frozen DDL was
    wrongly reported as ``no_op`` and the same-schema hard-reject gate never
    fired.

    The key is now the statement text itself. Only surrounding whitespace and a
    trailing statement terminator are dropped, because SQLite never stores them
    (``sqlite_master.sql`` keeps the body of the CREATE statement verbatim).
    Case and internal whitespace stay significant -- including everything inside
    string literals -- so such a schema fails closed as a conflict instead of
    silently passing. The comparison is deliberately conservative: an equivalent
    but differently written definition is rejected rather than accepted.
    """
    text = sql if sql is not None else ""
    text = text.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    return text


def _validate_connection(connection):
    if not isinstance(connection, sqlite3.Connection):
        raise PlanningSchemaError(
            "migrate_planning_schema requires a caller-supplied sqlite3.Connection; "
            "this migration never opens a database path itself"
        )


def _ensure_foreign_keys(connection):
    """Enable and verify SQLite foreign-key enforcement (frozen requirement)."""
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        row = connection.execute("PRAGMA foreign_keys").fetchone()
    except sqlite3.DatabaseError as exc:
        raise PlanningSchemaError(
            "could not enable SQLite foreign_keys enforcement: %s" % exc
        ) from exc
    if row is None or int(row[0]) != 1:
        raise PlanningSchemaError(
            "SQLite foreign_keys enforcement is not enabled; refusing to migrate"
        )


def _verify_base_table(connection):
    """Require the pre-existing ``workspace_projects(project_id PRIMARY KEY)``."""
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (BASE_PROJECT_TABLE,),
    ).fetchone()
    if row is None:
        raise PlanningSchemaError(
            "required base table %r is missing; refusing to migrate and refusing "
            "to create or modify it" % BASE_PROJECT_TABLE
        )
    info = connection.execute("PRAGMA table_info(%s)" % BASE_PROJECT_TABLE).fetchall()
    primary_key_columns = [column[1] for column in info if column[5]]
    if primary_key_columns != ["project_id"]:
        raise PlanningSchemaError(
            "%r must have project_id as its single PRIMARY KEY; found %r"
            % (BASE_PROJECT_TABLE, primary_key_columns)
        )


def _load_existing_objects(connection):
    names = tuple(_EXPECTED_OBJECTS)
    placeholders = ", ".join("?" for _ in names)
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE name IN (%s)" % placeholders,
        names,
    ).fetchall()
    return {row[1]: (row[0], row[2]) for row in rows}


def _read_declared_version(connection):
    try:
        rows = connection.execute(
            "SELECT singleton, version FROM planning_schema_meta"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise PlanningSchemaError(
            "planning_schema_meta exists but is not the frozen marker table; "
            "refusing to repair or overwrite it"
        ) from exc
    if len(rows) != 1:
        raise PlanningSchemaError(
            "planning_schema_meta must hold exactly one singleton row; found %d" % len(rows)
        )
    singleton, version = rows[0]
    if int(singleton) != 1:
        raise PlanningSchemaError(
            "planning_schema_meta singleton marker must be 1; found %r" % (singleton,)
        )
    return version


def _inspect_state(connection):
    """Return ``"absent"``, ``"current"`` or raise for partial/conflicting state."""
    existing = _load_existing_objects(connection)
    if not existing:
        return "absent"

    if "planning_schema_meta" in existing:
        version = _read_declared_version(connection)
        if int(version) != MIGRATION_VERSION:
            raise PlanningSchemaError(
                "unknown planning schema version %r; this migration only supports %d and "
                "never patches an unknown version" % (version, MIGRATION_VERSION)
            )

    missing = sorted(set(_EXPECTED_OBJECTS) - set(existing))
    if missing:
        raise PlanningSchemaError(
            "partial planning schema detected; refusing to repair it. present=%r missing=%r"
            % (sorted(existing), missing)
        )

    for name, (expected_kind, expected_sql) in _EXPECTED_OBJECTS.items():
        actual_kind, actual_sql = existing[name]
        if actual_kind != expected_kind:
            raise PlanningSchemaError(
                "schema conflict for %r: expected a %s but found a %s; refusing to overwrite"
                % (name, expected_kind, actual_kind)
            )
        if _normalize_sql(actual_sql) != _normalize_sql(expected_sql):
            raise PlanningSchemaError(
                "schema conflict for %r: existing definition differs from the frozen "
                "structure; refusing to repair or overwrite" % name
            )

    return "current"


def _apply(connection):
    try:
        connection.execute("BEGIN IMMEDIATE")
    except sqlite3.DatabaseError as exc:
        # Never roll back here: we did not start this transaction.
        raise PlanningSchemaError(
            "could not start the planning schema migration transaction: %s" % exc
        ) from exc

    try:
        for statement in _MIGRATION_STATEMENTS:
            connection.execute(statement)
    except Exception as exc:  # noqa: BLE001 - any step failure must roll back
        _rollback(connection)
        raise PlanningSchemaError(
            "planning schema migration failed and was rolled back: %s" % exc
        ) from exc

    try:
        connection.execute("COMMIT")
    except sqlite3.DatabaseError as exc:
        _rollback(connection)
        raise PlanningSchemaError(
            "planning schema migration could not commit and was rolled back: %s" % exc
        ) from exc


def _rollback(connection):
    try:
        connection.execute("ROLLBACK")
    except sqlite3.Error:
        pass


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def migrate_planning_schema(connection):
    """Create the frozen planning tables on a caller-supplied connection.

    Returns a dict with ``status`` (``"applied"`` or ``"no_op"``) and the created
    object names. Raises :class:`PlanningSchemaError` instead of repairing,
    overwriting or partially applying anything.
    """
    _validate_connection(connection)

    if connection.in_transaction:
        raise PlanningSchemaError(
            "the caller has an open transaction; commit or roll it back before migrating. "
            "This migration never commits the caller's transaction."
        )

    _ensure_foreign_keys(connection)
    _verify_base_table(connection)

    if _inspect_state(connection) == "current":
        return {
            "status": "no_op",
            "version": MIGRATION_VERSION,
            "created": [],
            "foreign_keys": True,
        }

    _apply(connection)

    return {
        "status": "applied",
        "version": MIGRATION_VERSION,
        "created": sorted(_EXPECTED_OBJECTS),
        "foreign_keys": True,
    }
