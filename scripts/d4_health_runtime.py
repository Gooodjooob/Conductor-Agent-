"""D4 board health-readiness collector candidate (G2-A runtime, read-only, offline).

Two-step read-only API for an already validated D4 board configuration
---------------------------------------------------------------------
1. ``freeze_identity(config, *, expected_board_sha256) -> FrozenIdentity``
   Freeze the startup identity once from a validated AppConfig (the exact nine G1
   serialized-config fields, supplied as a mapping or as an object exposing the G1
   attribute names) plus the trusted expected board-script SHA. The freeze records the
   board-script content digest and file identity, the configured source path digests
   plus ``(device, inode)``, the frozen config identity and the frozen data-source
   identity.
2. ``collect_health(frozen) -> dict``
   Re-measure the frozen inputs read-only and build the frozen
   ``d4-board-health.v1`` payload against that frozen identity.

Guarantees
----------
* Read-only: SQLite is opened through a ``mode=ro`` URI with ``PRAGMA query_only =
  ON``; this module never executes DDL, migration, repair, INSERT/UPDATE/DELETE or any
  other write, and it never creates or recreates a missing source. The board script and
  the catalog are opened ``O_RDONLY`` (with ``O_NOFOLLOW`` where the platform provides
  it) and only read.
* Bounded: every file read is capped (``MAX_BOARD_SCRIPT_BYTES`` /
  ``MAX_CATALOG_BYTES``), every SQLite statement carries a ``LIMIT`` and every database
  observation additionally installs one finite, deterministic VM-step budget
  (``MAX_SQLITE_VM_STEPS``) on the read-only connection *before any SQL runs*, shared by
  every statement of that observation. A ``LIMIT`` alone cannot bound a duplicate-heavy
  ``DISTINCT`` scan (SQLite may read an unbounded number of duplicate rows before it can
  emit the limited distinct rows), so an exhausted budget aborts the running statement and
  the observation fails closed. SQLite is opened with an explicit small connect timeout
  (``SQLITE_CONNECT_TIMEOUT_SECONDS``). There is no directory walk, no unrelated file
  inspection, no socket and no subprocess.
* Fail-closed: ``freeze_identity`` raises :class:`HealthRuntimeError` carrying only a
  generic code (never a path, SQL, token or exception text); ``collect_health`` never
  raises for source drift, it reports ``ready=false`` plus exactly one frozen reason
  code from the approved ``d4-board-health.v1`` enum.
* Mutable SQLite content/size/mtime never produce a false replacement signal: only the
  canonical path digest plus ``(device, inode)`` take part in the source identity, and
  the whole changing database is deliberately never hashed.
* Loopback-only assumption and no authentication: this module opens no socket, starts
  no service and claims no authentication. The payload carries ``auth="none"`` and the
  identity comparison is wrong-instance / misconfiguration detection only.

Frozen reason codes that can be emitted by ``collect_health``
------------------------------------------------------------
``COLLECT_REASONS``: ``ok``, ``source_missing``, ``source_unreadable``,
``source_replaced``, ``database_invalid``, ``catalog_invalid``, ``catalog_changed``,
``project_collision``, ``board_sha_unavailable``. Every one of them is a member of the
frozen reader reason enum (checked at import time); no free-form diagnostic is ever
produced.

Deterministic precedence used when several conditions hold at once
-----------------------------------------------------------------
``source_missing`` (a frozen input is gone) > ``board_sha_unavailable`` (the board
script content is not the expected digest, or cannot be measured within the read
bound) > the frozen reader's own source classification (> ``source_replaced`` >
``database_invalid`` > ``catalog_invalid`` > ``catalog_changed`` >
``project_collision``) > ``source_replaced`` (board-script file identity changed while
its content digest is unchanged) > ``ok``.

Honest limitations (not claimed as covered)
-------------------------------------------
* No database content fingerprint is taken, so an in-place database overwrite that
  preserves device/inode is not detected and ``source_changed`` is never emitted. The
  catalog is a small configuration-type file, so it is content-hashed and an in-place
  catalog edit is reported as ``catalog_changed``.
* Catalog snapshot material files referenced from inside the catalog are opened read-only
  with descriptor-relative traversal: every relative component is opened
  ``O_DIRECTORY``/``O_NOFOLLOW`` and the final component ``O_RDONLY``/``O_NOFOLLOW``, so a
  symlink at *any* level is refused -- including a symlink ancestor that resolves back
  inside the catalog parent, which a path-resolution containment comparison cannot catch --
  and no mutable ancestor is ever followed. Each material is bounded, must be an existing
  regular file and is verified against its declared lowercase sha256; any missing, invalid,
  changed, oversized, non-UTF8, control-character or symlinked material makes the
  observation ``catalog_invalid``. Control characters (including NUL), format characters and
  unpaired surrogates are rejected on the path string itself, before any filesystem
  operation, so a hostile catalog can never escape as a raw ``ValueError``/``UnicodeError``.
  The catalog ``sourcePath`` field is never opened.
* Project-collision detection requires the supplied storage-contract tables and columns
  (``REQUIRED_SCHEMA``, derived from the exact current D4 ``SqliteStore`` schema) and
  scans DISTINCT ``workspace_projects.project_id`` / ``tasks.project_id`` with a
  limit-plus-one completeness bound *and* the shared finite VM-step budget of the
  observation, because a ``LIMIT`` alone does not bound a duplicate-heavy ``DISTINCT`` scan.
  A scan that overflows the row bound, exhausts the step budget or fails is reported as
  ``database_invalid`` instead of a silently truncated project set, and no partial project
  set is ever used; the whole changing database is still never hashed.
* A database whose header advertises WAL journalling is refused before any connection is
  opened, because a read-only open could create or recover ``-wal``/``-shm`` sidecars; it
  is reported as ``source_unreadable`` (fail-closed, no repair, never ``immutable=1``).
* This module is a report-only readiness collector: it does not implement ``/healthz``,
  does not wire any endpoint or socket, does not start/stop/restart any service or
  launch agent, performs no install, migration, network, Git, sync or business write,
  and it makes no complete-MVP claim.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import json
import os
import re
import sqlite3
import stat
import unicodedata
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "FrozenIdentity",
    "HealthRuntimeError",
    "freeze_identity",
    "collect_health",
]


# --------------------------------------------------------------------------- #
# Frozen protocol reader (the approved candidate reader, reused unchanged)
# --------------------------------------------------------------------------- #
_READER_FILENAME = "d4_health_reader.py"
_READER_PATH = Path(__file__).resolve().parent / _READER_FILENAME
_READER_SPEC = importlib.util.spec_from_file_location("d4_health_reader", _READER_PATH)
if _READER_SPEC is None or _READER_SPEC.loader is None:  # pragma: no cover - defensive
    raise ImportError("cannot load the frozen health reader module")
_reader = importlib.util.module_from_spec(_READER_SPEC)
_READER_SPEC.loader.exec_module(_reader)
del _READER_SPEC

# Frozen protocol literals and the exact nine G1 serialized-config field names.
CONFIG_FIELDS = _reader.CONFIG_FIELDS
CONFIG_ATTRIBUTES = (
    "schema_version",
    "host",
    "port",
    "db_path",
    "catalog_path",
    "python_path",
    "board_script",
    "runtime_dir",
    "label",
)
SOURCE_SLOTS = ("db", "catalog")
HEALTH_SCHEMA = _reader.HEALTH_SCHEMA
SERVICE_NAME = _reader.SERVICE_NAME
AUTH_NONE = _reader.AUTH_NONE
HEALTH_REASONS = frozenset(_reader.REASONS)

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

FREEZE_ERROR_CODES = (
    "invalid_config",
    "invalid_expected_board_sha256",
    "source_missing",
    "source_unreadable",
    "board_sha_unavailable",
    "invalid_frozen_identity",
)

if not set(COLLECT_REASONS) <= HEALTH_REASONS:  # pragma: no cover - defensive
    raise ImportError("collector reasons must be a subset of the frozen reason enum")

LOWER_HEX = frozenset("0123456789abcdef")
READ_CHUNK = 65536
MAX_BOARD_SCRIPT_BYTES = 4 * 1024 * 1024
MAX_CATALOG_BYTES = 2 * 1024 * 1024
MAX_SCHEMA_TABLES = 512
MAX_IDENTITY_ROWS = 1024
MAX_CATALOG_MATERIALS = 256
MAX_CATALOG_MATERIAL_BYTES = 256 * 1024
MAX_CATALOG_TOTAL_MATERIAL_BYTES = 4 * 1024 * 1024

# Bounded SQLite execution. ``LIMIT`` cannot bound a duplicate-heavy ``DISTINCT`` scan:
# SQLite may read an unbounded number of duplicate rows before it can emit the limited
# distinct rows, so the row bound alone leaves the observation open to unbounded work. Each
# observation therefore also installs one finite, deterministic virtual-machine step budget
# on its read-only connection through ``set_progress_handler`` *before any SQL runs*. The
# single counter is shared by every statement of that observation (schema listing, both
# project scans); exhausting it aborts the running statement with SQLite's ``interrupted``
# error, which the collector maps to a fail-closed ``database_invalid`` -- never to a ready
# database and never to a partial project set. The whole changing database is still never
# hashed, ``immutable=1`` is still never used and the read-only/WAL refusal is unchanged.
MAX_SQLITE_VM_STEPS = 1_000_000
_DEFAULT_MAX_SQLITE_VM_STEPS = 1_000_000
SQLITE_PROGRESS_INTERVAL = 1
SQLITE_CONNECT_TIMEOUT_SECONDS = 0.5

CATALOG_SCHEMA = "project-catalog.v1"
CATALOG_STATUSES = frozenset({
    "verified",
    "validated",
    "partial",
    "in_progress",
    "blocked",
    "planned",
    "awaiting_review",
})
CATALOG_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")

# Tables and columns of the supplied D4 storage contract, derived from the exact current
# ``SqliteStore`` schema (``inputs/required-schema.json``). A database that is missing any
# required table or column cannot serve the read models and is structurally invalid; extra
# tables and extra columns are allowed.
REQUIRED_SCHEMA = {
    "sources": frozenset({"source_id", "label", "capability_json", "registered_at"}),
    "agents": frozenset({"agent_id", "label", "note", "registered_at"}),
    "tasks": frozenset({
        "task_id", "project_id", "title", "agent_id", "source_id", "workspace",
        "baseline", "goal", "acceptance_json", "permissions_json", "limits_json",
        "status", "revision", "current_attempt_id", "current_delivery_id",
        "closure_json", "created_at", "updated_at",
    }),
    "attempts": frozenset({
        "attempt_id", "task_id", "ordinal", "state", "created_at", "submitted_at",
        "accepted_by_host_at", "started_at", "terminal_at", "terminal_kind",
        "blocked_json", "note", "submit_state", "submission_json",
    }),
    "events": frozenset({
        "event_id", "task_id", "attempt_id", "project_id", "source_id", "agent_id",
        "sequence", "type", "occurred_at", "received_at", "content_hash", "outcome",
        "outcome_code", "reason", "result_json", "synthetic",
    }),
    "deliveries": frozenset({
        "delivery_id", "task_id", "attempt_id", "summary", "changed_files_json",
        "verification_json", "risks_json", "report_path", "unfinished_json",
        "source_delivery_id", "created_at",
    }),
    "reviews": frozenset({
        "review_id", "task_id", "delivery_id", "decision", "note", "review_round",
        "resulting_attempt_id", "reviewer_id", "created_at",
    }),
    "activity": frozenset({
        "activity_id", "task_id", "attempt_id", "at", "kind", "detail_json",
    }),
    "reconciliation": frozenset({
        "recon_id", "task_id", "event_id", "attempt_id", "sequence", "kind",
        "detail_json", "at",
    }),
    "conflicts": frozenset({"conflict_id", "event_id", "task_id", "detail_json", "at"}),
    "reports": frozenset({
        "report_id", "report_revision", "report_type", "project_id", "task_id",
        "attempt_id", "delivery_id", "source_card_json", "requested_by_json",
        "executed_by_json", "reviewed_by_json", "adapter", "session_id", "host_run_id",
        "board_summary_json", "business_summary_json", "evidence_json",
        "canonical_digest", "source_file_json", "imported_at", "imported_by_json",
        "synthetic",
    }),
    "task_source_cards": frozenset({
        "task_id", "project_id", "card_id", "card_revision", "source_path",
        "source_sha256", "canonical_digest", "registered_json", "registered_by_json",
        "created_at",
    }),
    "workspace_projects": frozenset({
        "project_id", "name", "goal", "status", "current_stage", "owner_id",
        "next_step", "source_document", "source_sha256", "created_at", "updated_at",
    }),
    "workspace_workstreams": frozenset({
        "workstream_id", "project_id", "name", "goal", "status", "position",
        "source_section", "next_step", "updated_at",
    }),
    "workspace_milestones": frozenset({
        "milestone_id", "project_id", "workstream_id", "name", "status", "gate",
        "position", "updated_at",
    }),
    "workspace_workers": frozenset({
        "worker_id", "display_name", "role", "host", "environment", "description",
        "default_model", "registration_status", "runtime_status", "enabled",
        "read_scope_json", "write_scope_json", "allowed_projects_json",
        "binding_revision", "last_checked_at", "updated_at",
    }),
    "workspace_task_links": frozenset({
        "task_id", "project_id", "workstream_id", "milestone_id", "worker_id",
        "updated_at",
    }),
    "workspace_plan_sources": frozenset({
        "source_id", "project_id", "title", "path", "source_sha256", "imported_at",
        "status",
    }),
}
PROJECT_ID_TABLES = ("workspace_projects", "tasks")

_SQLITE_READ_ONLY_SUFFIX = "?mode=ro"


# --------------------------------------------------------------------------- #
# Public error type: generic codes only
# --------------------------------------------------------------------------- #
class HealthRuntimeError(Exception):
    """Fail-closed freeze error carrying exactly one generic code.

    ``str(error)`` is the code itself: no path, SQL, token or underlying exception text
    can reach the caller or any report. Raise sites use ``from None`` so a traceback
    never embeds the original operating-system message either.
    """

    def __init__(self, code: str) -> None:
        if code not in FREEZE_ERROR_CODES:
            raise ValueError("invalid error code")
        self.code = code
        super().__init__(code)


# --------------------------------------------------------------------------- #
# Frozen identity returned by the first step
# --------------------------------------------------------------------------- #
@dataclasses.dataclass(frozen=True)
class FrozenIdentity:
    """Startup identity frozen once and used by every later ``collect_health`` call.

    The nested mappings are private copies; callers must treat them as read-only.
    """

    board_sha256: str
    config_identity: str
    data_source_identity: str
    config: Dict[str, Any]
    board_script: Dict[str, Any]
    sources: Dict[str, Any]


# --------------------------------------------------------------------------- #
# Scalar helpers
# --------------------------------------------------------------------------- #
def _is_lower_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in LOWER_HEX for char in value)


def _path_digest(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


def _lstat(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _file_identity(path: str, info: os.stat_result) -> Dict[str, Any]:
    return {
        "pathSha256": _path_digest(path),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


def _config_fields(config: Any) -> Dict[str, Any]:
    """Normalize an AppConfig (mapping or G1 attribute object) to the nine G1 fields."""
    if isinstance(config, (str, bytes, bytearray)):
        raise HealthRuntimeError("invalid_config")
    if isinstance(config, Mapping):
        if set(config.keys()) != set(CONFIG_FIELDS):
            raise HealthRuntimeError("invalid_config")
        return {name: config[name] for name in CONFIG_FIELDS}
    fields: Dict[str, Any] = {}
    for name, attribute in zip(CONFIG_FIELDS, CONFIG_ATTRIBUTES):
        try:
            fields[name] = getattr(config, attribute)
        except AttributeError:
            raise HealthRuntimeError("invalid_config") from None
    return fields


# --------------------------------------------------------------------------- #
# Bounded, read-only measurement primitives
# --------------------------------------------------------------------------- #
class _ReadBoundExceeded(Exception):
    """Internal marker: the bounded read limit was reached (never escapes)."""


class _ReadFailed(Exception):
    """Internal marker: the read-only open/read failed (never escapes)."""


class _SqliteStepBudget:
    """One finite deterministic VM-step budget for one read-only observation.

    The handler is installed with ``set_progress_handler`` before any SQL is executed and
    is shared by every statement of the observation. Returning non-zero aborts the running
    statement with SQLite's ``interrupted`` error, which the observation maps to a
    fail-closed outcome instead of continuing to scan duplicate rows without bound.
    """

    __slots__ = ("max_steps", "steps", "exhausted")

    def __init__(self, max_steps: int) -> None:
        self.max_steps = max_steps
        self.steps = 0
        self.exhausted = False

    def handler(self) -> int:
        self.steps += 1
        if self.steps > self.max_steps:
            self.exhausted = True
            return 1
        return 0


def _step_budget() -> int:
    """The effective per-observation VM-step budget (fail-safe on a misconfigured value)."""
    budget = MAX_SQLITE_VM_STEPS
    if isinstance(budget, bool) or not isinstance(budget, int) or budget <= 0:
        return _DEFAULT_MAX_SQLITE_VM_STEPS
    return budget


def _attach_step_budget(connection: sqlite3.Connection) -> _SqliteStepBudget:
    """Install the shared VM-step budget on one connection before any SQL statement."""
    budget = _SqliteStepBudget(_step_budget())
    connection.set_progress_handler(budget.handler, SQLITE_PROGRESS_INTERVAL)
    return budget


def _read_fd_bounded(descriptor: int, limit: int) -> bytes:
    """Read at most ``limit`` bytes from an already opened descriptor, regular files only."""
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _ReadFailed()
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise _ReadBoundExceeded()
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        raise _ReadFailed() from None


def _bounded_read(path: str, limit: int) -> bytes:
    """Read at most ``limit`` bytes of one existing regular file, read-only."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _ReadFailed() from None
    try:
        return _read_fd_bounded(descriptor, limit)
    except OSError:
        raise _ReadFailed() from None
    finally:
        os.close(descriptor)


def _read_prefix(path: str, length: int) -> Optional[bytes]:
    """Read at most ``length`` leading bytes of one regular file, read-only."""
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        return os.read(descriptor, length)
    except OSError:
        return None
    finally:
        os.close(descriptor)


def _is_wal_header(path: str) -> bool:
    """True when the SQLite header advertises WAL journalling (bytes 18/19 equal 2)."""
    prefix = _read_prefix(path, 100)
    if prefix is None or len(prefix) < 100:
        return False
    return prefix[18] == 2 or prefix[19] == 2


def _table_columns(connection: sqlite3.Connection, table: str) -> Optional[frozenset]:
    """Read-only column-name set for one required table, or ``None`` on any SQLite error."""
    try:
        rows = connection.execute("PRAGMA table_info(%s)" % table).fetchall()
    except sqlite3.Error:
        return None
    return frozenset(row[1] for row in rows if len(row) > 1)


def _verify_schema(connection: sqlite3.Connection) -> Optional[frozenset]:
    """Return the present-table set when every required table/column exists, else ``None``.

    A table listing that cannot be proven complete within ``MAX_SCHEMA_TABLES``, any
    SQLite error and any missing required table or column all return ``None`` so the
    caller reports the database as structurally invalid (fail-closed).
    """
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' LIMIT ?",
            (MAX_SCHEMA_TABLES + 1,),
        ).fetchall()
    except sqlite3.Error:
        return None
    if len(rows) > MAX_SCHEMA_TABLES:
        return None
    tables = frozenset(row[0] for row in rows if isinstance(row[0], str))
    if not set(REQUIRED_SCHEMA).issubset(tables):
        return None
    for table, required in REQUIRED_SCHEMA.items():
        columns = _table_columns(connection, table)
        if columns is None or not required.issubset(columns):
            return None
    return tables


def _board_script_digest(path: str) -> str:
    """Return the board-script content sha256 within the read bound (fail-closed)."""
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise HealthRuntimeError("board_sha_unavailable")
    try:
        raw = _bounded_read(path, MAX_BOARD_SCRIPT_BYTES)
    except (_ReadFailed, _ReadBoundExceeded):
        raise HealthRuntimeError("board_sha_unavailable") from None
    return hashlib.sha256(raw).hexdigest()


def _project_ids(connection: sqlite3.Connection, tables: frozenset) -> Tuple[frozenset, bool]:
    """Bounded read-only collection of live project ids from the supplied schema.

    ``DISTINCT`` with a limit-plus-one makes completeness decidable: when more than
    ``MAX_IDENTITY_ROWS`` distinct project ids exist in one contract table the scan
    cannot be proven complete and returns ``complete=False`` (the caller then fails
    closed) instead of returning a silently truncated set. Any SQLite error also returns
    ``complete=False`` rather than an empty set.
    """
    collected = set()
    for table in PROJECT_ID_TABLES:
        if table not in tables:
            continue
        try:
            rows = connection.execute(
                "SELECT DISTINCT project_id FROM %s LIMIT ?" % table,
                (MAX_IDENTITY_ROWS + 1,),
            ).fetchall()
        except sqlite3.Error:
            return frozenset(), False
        if len(rows) > MAX_IDENTITY_ROWS:
            return frozenset(), False
        for row in rows:
            value = row[0]
            if isinstance(value, str) and value != "":
                collected.add(value)
    return frozenset(collected), True


def _observe_database(path: str) -> Dict[str, Any]:
    """Read-only observation of one configured SQLite source (never creates it).

    The connection carries one finite VM-step budget shared by every statement of this
    observation; ``vmSteps`` is the internal bounded diagnostic recording how many VM steps
    were actually executed. It is never copied into any snapshot or health payload.
    """
    blank = {
        "present": False,
        "readable": False,
        "metadata": None,
        "verified": False,
        "vmSteps": 0,
    }
    info = _lstat(path)
    if info is None:
        return dict(blank, project_ids=frozenset())
    metadata = _file_identity(path, info)
    observation = {
        "present": True,
        "readable": False,
        "metadata": metadata,
        "verified": False,
        "vmSteps": 0,
        "project_ids": frozenset(),
    }
    if not stat.S_ISREG(info.st_mode):
        return observation

    # A WAL database cannot be proven readable/side-effect-free through a read-only open
    # (SQLite may create or recover ``-wal``/``-shm``), so it is refused before any
    # connection is opened. ``immutable=1`` is never used to ignore a live WAL.
    if _is_wal_header(path):
        return observation

    uri = Path(os.path.abspath(path)).as_uri() + _SQLITE_READ_ONLY_SUFFIX
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        )
    except sqlite3.OperationalError:
        return observation
    except sqlite3.DatabaseError:
        # Readable bytes that are not a SQLite database: structurally invalid.
        return dict(observation, readable=True)
    budget = None
    try:
        # The VM-step budget is installed before any SQL: the pragma, the schema listing and
        # both project scans share the one counter, so a duplicate-heavy scan that would
        # never be bounded by ``LIMIT`` aborts here instead of running unbounded.
        budget = _attach_step_budget(connection)
        connection.execute("PRAGMA query_only = ON")
        tables = _verify_schema(connection)
        if tables is None:
            return dict(observation, readable=True, vmSteps=budget.steps)
        project_ids, complete = _project_ids(connection, tables)
        if not complete:
            # An unprovable, failing or budget-exhausted project scan must never yield a
            # ready database and must never report a partial project set.
            return dict(observation, readable=True, vmSteps=budget.steps)
        return dict(
            observation,
            readable=True,
            verified=True,
            vmSteps=budget.steps,
            project_ids=project_ids,
        )
    except sqlite3.OperationalError:
        # Includes the budget's own ``interrupted`` abort: fail closed, never partial.
        return dict(observation, vmSteps=budget.steps if budget is not None else 0)
    except sqlite3.DatabaseError:
        return dict(observation, readable=True, vmSteps=budget.steps if budget is not None else 0)
    finally:
        try:
            connection.set_progress_handler(None, 0)
        except sqlite3.Error:  # pragma: no cover - defensive
            pass
        connection.close()


def _safe_catalog_material(snapshot: Any) -> bool:
    """True for a relative catalog material path with no control char or bad segment.

    The check is performed on the string alone, before any filesystem operation: control
    characters (including NUL), format characters and unpaired surrogates are refused here,
    so a hostile catalog can never reach ``os.open`` with an unencodable name (which would
    escape as a raw ``ValueError``/``UnicodeError`` instead of the frozen
    ``catalog_invalid``). Absolute paths and ``.``/``..``/empty segments are refused as well.
    """
    if not isinstance(snapshot, str) or snapshot == "" or snapshot.startswith("/"):
        return False
    for char in snapshot:
        if char == "\x00" or unicodedata.category(char).startswith("C"):
            return False
    for part in snapshot.split("/"):
        if part in ("", ".", ".."):
            return False
    return True


def _directory_flags() -> int:
    """Flags for opening one catalog material *parent* component: directory, never a symlink."""
    return os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _material_descriptor(parent: str, snapshot: str) -> Optional[int]:
    """Open one catalog material with descriptor-relative, symlink-refusing traversal.

    The trusted configured catalog directory is opened read-only as a directory; every
    relative component of the material path is then opened ``O_DIRECTORY``/``O_NOFOLLOW``
    relative to the previous descriptor and the final component ``O_RDONLY``/``O_NOFOLLOW``.
    A symlink is therefore refused at *every* level -- including a symlink ancestor that
    resolves back inside the catalog parent, which a path-resolution containment comparison
    would accept -- and no mutable ancestor is ever followed. Every descriptor opened here is
    closed before returning, on the success and on every failure path alike; the returned
    file descriptor (or ``None``) is owned by the caller.
    """
    file_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    parents = []
    final = None
    try:
        current = os.open(parent, _directory_flags())
        parents.append(current)
        parts = snapshot.split("/")
        for part in parts[:-1]:
            current = os.open(part, _directory_flags(), dir_fd=current)
            parents.append(current)
        final = os.open(parts[-1], file_flags, dir_fd=current)
    except (OSError, ValueError):
        final = None
    finally:
        for descriptor in parents:
            try:
                os.close(descriptor)
            except OSError:  # pragma: no cover - defensive
                pass
    return final


def _material_bytes(parent: str, snapshot: str) -> Optional[bytes]:
    """Bounded read-only bytes of one catalog material confined to the catalog parent.

    Returns ``None`` when the material path is unsafe (absolute, ``.``/``..``/empty segment
    or a control character), when any relative component -- or the final file -- is a
    symlink, when the target is not an existing bounded regular file, when the
    descriptor-relative open or read fails, when the file exceeds
    ``MAX_CATALOG_MATERIAL_BYTES`` or when the content is not valid UTF-8. The declared
    ``sourcePath`` is never used.
    """
    if not _safe_catalog_material(snapshot):
        return None
    descriptor = _material_descriptor(parent, snapshot)
    if descriptor is None:
        return None
    try:
        raw = _read_fd_bounded(descriptor, MAX_CATALOG_MATERIAL_BYTES)
    except (_ReadFailed, _ReadBoundExceeded):
        return None
    finally:
        os.close(descriptor)
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return raw


def _validate_catalog(data: Any, parent: str) -> Optional[frozenset]:
    """Structural check of one parsed catalog document (bounded, read-only).

    Mirrors the supplied read-only catalog contract: schema version, project/task/
    milestone identifier rules, task status and milestone membership, document list
    shape and issue-task membership. Every catalog material snapshot must be a safe
    relative path confined to ``parent``, carry a lowercase sha256 and byte-match that
    digest as an existing bounded UTF-8 regular file; any missing, invalid, changed,
    oversized or non-UTF8 material makes the catalog invalid. Returns the project-id set
    when structurally valid, otherwise ``None``.
    """
    if not isinstance(data, dict) or data.get("schemaVersion") != CATALOG_SCHEMA:
        return None
    rows = data.get("projects")
    if not isinstance(rows, list):
        return None
    project_ids = set()
    task_ids = set()
    materials = []
    for project in rows:
        if not isinstance(project, dict):
            return None
        project_id = project.get("projectId")
        if not _catalog_identifier(project_id) or project_id in project_ids:
            return None
        project_ids.add(project_id)
        name = project.get("name")
        if not isinstance(name, str) or not name.strip():
            return None
        milestones = project.get("milestones", [])
        if not isinstance(milestones, list):
            return None
        milestone_ids = set()
        for milestone in milestones:
            if not isinstance(milestone, dict):
                return None
            milestone_id = milestone.get("id")
            if not _catalog_identifier(milestone_id) or milestone_id in milestone_ids:
                return None
            milestone_ids.add(milestone_id)
        tasks = project.get("tasks")
        if not isinstance(tasks, list):
            return None
        project_task_ids = set()
        for task in tasks:
            if not isinstance(task, dict):
                return None
            task_id = task.get("taskId")
            if not _catalog_identifier(task_id) or task_id in task_ids:
                return None
            task_ids.add(task_id)
            project_task_ids.add(task_id)
            if task.get("status") not in CATALOG_STATUSES:
                return None
            if task.get("milestoneId") not in milestone_ids:
                return None
            documents = task.get("documents", [])
            if not isinstance(documents, list):
                return None
            for document in documents:
                if not isinstance(document, dict):
                    return None
                snapshot = document.get("snapshot")
                if not _safe_catalog_material(snapshot):
                    return None
                declared = document.get("sha256")
                if not _is_lower_hex64(declared):
                    return None
                materials.append((snapshot, declared))
        issues = project.get("issues", [])
        if not isinstance(issues, list):
            return None
        for issue in issues:
            if not isinstance(issue, dict) or issue.get("taskId") not in project_task_ids:
                return None
    if len(materials) > MAX_CATALOG_MATERIALS:
        return None
    total = 0
    for snapshot, declared in materials:
        raw = _material_bytes(parent, snapshot)
        if raw is None:
            return None
        total += len(raw)
        if total > MAX_CATALOG_TOTAL_MATERIAL_BYTES:
            return None
        if hashlib.sha256(raw).hexdigest() != declared:
            return None
    return frozenset(project_ids)


def _catalog_identifier(value: Any) -> bool:
    return isinstance(value, str) and CATALOG_IDENTIFIER.fullmatch(value) is not None


def _observe_catalog(path: str) -> Dict[str, Any]:
    """Read-only observation of one configured catalog source (bounded content read)."""
    info = _lstat(path)
    if info is None:
        return {
            "present": False,
            "readable": False,
            "metadata": None,
            "contentSha256": None,
            "verified": False,
            "project_ids": frozenset(),
        }
    observation = {
        "present": True,
        "readable": False,
        "metadata": _file_identity(path, info),
        "contentSha256": None,
        "verified": False,
        "project_ids": frozenset(),
    }
    if not stat.S_ISREG(info.st_mode):
        return observation
    try:
        raw = _bounded_read(path, MAX_CATALOG_BYTES)
    except _ReadFailed:
        return observation
    except _ReadBoundExceeded:
        # An oversized catalog cannot be hashed or validated within the read bound.
        return dict(observation, readable=True)
    observation = dict(observation, readable=True, contentSha256=hashlib.sha256(raw).hexdigest())
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return observation
    project_ids = _validate_catalog(data, os.path.dirname(path))
    if project_ids is None:
        return observation
    return dict(observation, verified=True, project_ids=project_ids)


# --------------------------------------------------------------------------- #
# Snapshot assembly helpers
# --------------------------------------------------------------------------- #
def _copy_frozen_record(record: Optional[Mapping]) -> Optional[Dict[str, Any]]:
    if record is None:
        return None
    return {
        "metadata": dict(record["metadata"]),
        "contentSha256": record["contentSha256"],
    }


def _observed_db_entry(observation: Mapping) -> Dict[str, Any]:
    metadata = observation["metadata"]
    return {
        "present": observation["present"],
        "readable": observation["readable"],
        "metadata": dict(metadata) if metadata is not None else None,
        "contentSha256": None,
        "verified": observation["verified"],
    }


def _observed_catalog_entry(observation: Mapping, *, collision: bool) -> Dict[str, Any]:
    metadata = observation["metadata"]
    return {
        "present": observation["present"],
        "readable": observation["readable"],
        "metadata": dict(metadata) if metadata is not None else None,
        "contentSha256": observation["contentSha256"],
        "verified": observation["verified"],
        "projectCollision": collision,
    }


def _board_script_state(frozen: FrozenIdentity) -> Tuple[Optional[str], bool]:
    """Return the current board-script digest (or ``None``) and its identity drift."""
    path = frozen.config["boardScript"]
    try:
        digest = _board_script_digest(path)
    except HealthRuntimeError:
        return None, False
    info = _lstat(path)
    if info is None:  # pragma: no cover - defensive
        return None, False
    changed = (
        _path_digest(path) != frozen.board_script["pathSha256"]
        or int(info.st_dev) != frozen.board_script["device"]
        or int(info.st_ino) != frozen.board_script["inode"]
    )
    return digest, changed


# --------------------------------------------------------------------------- #
# Step 1: freeze the startup identity
# --------------------------------------------------------------------------- #
def freeze_identity(config: Any, *, expected_board_sha256: Any) -> FrozenIdentity:
    """Freeze the startup identity from a validated AppConfig and the trusted SHA.

    ``config`` is either a mapping with the exact nine G1 serialized-config fields or an
    AppConfig-like object exposing the matching G1 attribute names. The board script is
    read once within the byte bound and its content digest must equal the trusted
    ``expected_board_sha256``. Every configured source must exist and be readable; its
    canonical path digest plus ``(device, inode)`` are frozen, and the catalog content
    digest is frozen as well.

    Raises :class:`HealthRuntimeError` with one of ``FREEZE_ERROR_CODES``. No file is
    ever created, modified or removed: a missing source stays missing.
    """
    if not _is_lower_hex64(expected_board_sha256):
        raise HealthRuntimeError("invalid_expected_board_sha256")

    fields = _config_fields(config)
    try:
        config_identity = _reader.compute_config_identity(
            fields, board_sha256=expected_board_sha256
        )
    except ValueError:
        raise HealthRuntimeError("invalid_config") from None

    board_script_path = fields["boardScript"]
    board_digest = _board_script_digest(board_script_path)
    if board_digest != expected_board_sha256:
        raise HealthRuntimeError("board_sha_unavailable")
    board_info = _lstat(board_script_path)
    if board_info is None:  # pragma: no cover - defensive
        raise HealthRuntimeError("board_sha_unavailable")
    board_script = dict(
        _file_identity(board_script_path, board_info), contentSha256=board_digest
    )

    sources: Dict[str, Any] = {"db": None, "catalog": None}
    metadata: Dict[str, Any] = {"db": None, "catalog": None}

    db_path = fields["dbPath"]
    if db_path is not None:
        observation = _observe_database(db_path)
        if not observation["present"]:
            raise HealthRuntimeError("source_missing")
        if not observation["readable"]:
            raise HealthRuntimeError("source_unreadable")
        metadata["db"] = dict(observation["metadata"])
        sources["db"] = {"metadata": dict(observation["metadata"]), "contentSha256": None}

    catalog_path = fields["catalogPath"]
    if catalog_path is not None:
        observation = _observe_catalog(catalog_path)
        if not observation["present"]:
            raise HealthRuntimeError("source_missing")
        if observation["contentSha256"] is None:
            raise HealthRuntimeError("source_unreadable")
        metadata["catalog"] = dict(observation["metadata"])
        sources["catalog"] = {
            "metadata": dict(observation["metadata"]),
            "contentSha256": observation["contentSha256"],
        }

    data_source_identity = _reader.compute_data_source_identity(
        db=metadata["db"], catalog=metadata["catalog"]
    )
    return FrozenIdentity(
        board_sha256=expected_board_sha256,
        config_identity=config_identity,
        data_source_identity=data_source_identity,
        config=dict(fields),
        board_script=board_script,
        sources=sources,
    )


# --------------------------------------------------------------------------- #
# Step 2: collect the current read-only readiness payload
# --------------------------------------------------------------------------- #
def collect_health(frozen: Any) -> Dict[str, Any]:
    """Collect one ``d4-board-health.v1`` payload against a frozen identity.

    Re-measures the frozen inputs read-only (bounded file reads, read-only SQLite), then
    classifies the result against the frozen identity with the documented precedence
    and returns a payload validated by the frozen protocol validator. Source drift is
    reported, never repaired: nothing is created, written, migrated or mutated.

    Raises :class:`HealthRuntimeError("invalid_frozen_identity")` when the argument is
    not a :class:`FrozenIdentity` produced by :func:`freeze_identity`.
    """
    if not isinstance(frozen, FrozenIdentity):
        raise HealthRuntimeError("invalid_frozen_identity")

    config = frozen.config
    db_path = config["dbPath"]
    catalog_path = config["catalogPath"]

    db_observation = None
    observed_db = None
    if db_path is not None:
        db_observation = _observe_database(db_path)
        observed_db = _observed_db_entry(db_observation)

    observed_catalog = None
    if catalog_path is not None:
        catalog_observation = _observe_catalog(catalog_path)
        collision = False
        if (
            catalog_observation["verified"]
            and db_observation is not None
            and db_observation["readable"]
        ):
            collision = bool(
                catalog_observation["project_ids"] & db_observation["project_ids"]
            )
        observed_catalog = _observed_catalog_entry(catalog_observation, collision=collision)

    snapshot = {
        "schemaVersion": _reader.SNAPSHOT_SCHEMA,
        "boardSha256": frozen.board_sha256,
        "config": dict(config),
        "frozen": {
            "db": _copy_frozen_record(frozen.sources["db"]),
            "catalog": _copy_frozen_record(frozen.sources["catalog"]),
        },
        "observed": {"db": observed_db, "catalog": observed_catalog},
    }

    payload = _reader.build_health_payload(snapshot)
    reason = payload["reason"]
    if reason != "source_missing":
        board_digest, board_identity_changed = _board_script_state(frozen)
        if board_digest != frozen.board_sha256:
            reason = "board_sha_unavailable"
        elif reason == "ok" and board_identity_changed:
            reason = "source_replaced"

    if reason == payload["reason"]:
        return payload

    collected = {
        "schemaVersion": HEALTH_SCHEMA,
        "service": SERVICE_NAME,
        "boardSha256": frozen.board_sha256,
        "configIdentity": frozen.config_identity,
        "dataSource": {
            "kind": payload["dataSource"]["kind"],
            "present": payload["dataSource"]["present"],
            "identity": payload["dataSource"]["identity"],
        },
        "ready": reason == "ok",
        "reason": reason,
        "auth": AUTH_NONE,
    }
    return _reader.validate_health_payload(collected)
