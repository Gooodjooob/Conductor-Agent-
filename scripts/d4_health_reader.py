"""D4 board health-reader / source-drift candidate (offline, no application I/O).

Candidate scope (G2-B reader layer, offline only)
-------------------------------------------------
This module adds a *narrow, offline reader layer* on top of the frozen G2-A pure
health/identity protocol. It consumes **already supplied trusted snapshots** -- plain
Python objects handed in by a future trusted caller -- and never performs application
I/O itself: it does not ``stat``/open/read/write any file, never touches sqlite, never
opens a socket, never spawns a process and never starts, parses or installs launchd or
HTTP infrastructure. Everything here is deterministic and pure; supplied inputs are
never mutated.

Frozen protocol semantics are preserved verbatim (same constants, same canonical-bytes
rule, same strict types, same validation and fail-closed evaluation order):

1. ``compute_config_identity(config, *, board_sha256) -> str``
2. ``compute_data_source_identity(*, db=None, catalog=None) -> str``
3. ``validate_health_payload(payload) -> dict``
4. ``evaluate_health_payload(payload, *, expected_board_sha256,
   expected_config_identity, expected_data_source_identity) -> (bool, str)``

Reader layer added here
-----------------------
5. ``validate_source_snapshot(snapshot) -> dict``
   Fail-closed syntax validation of one trusted snapshot. Returns a fresh copy whose
   outer object and nested objects are all new. Any malformed snapshot raises
   ``ValueError`` with a generic diagnostic that never carries a path, token or raw
   exception text.
6. ``classify_source_drift(snapshot) -> str``
   Deterministic classification of the configured source set into exactly one frozen
   reason code (``ok`` or one of the frozen failure codes).
7. ``build_health_payload(snapshot) -> dict``
   Bounded construction of the frozen eight-key ``d4-board-health.v1`` payload. The
   result is re-validated by ``validate_health_payload`` before being returned, so it
   can never carry an unknown field, a path, a token or free-form text.
8. ``serialize_health_payload(payload) -> bytes``
   Validate plus canonical-bytes encoding. Output is bounded: the validator admits
   exactly eight keys whose values are fixed-width digests, booleans, fixed literals or
   enum codes, so the encoded size is a small constant independent of supplied data.
9. ``evaluate_source_snapshot(snapshot, *, expected_board_sha256,
   expected_config_identity, expected_data_source_identity) -> (bool, str)``
   Snapshot-level wrapper over the frozen evaluator: an invalid snapshot returns
   ``(False, "invalid_payload")``; otherwise the frozen order applies (board, then
   config, then data source, then readiness reason). This is configured-instance /
   misconfiguration detection, NOT authentication.

Trusted snapshot schema (exact keys; all digest values are 64 lower-case hex)
----------------------------------------------------------------------------
``{"schemaVersion": "d4-source-snapshot.v1", "boardSha256": <64 hex>,
  "config": <fresh exact nine G1 config fields>,
  "frozen": {"db": <frozen source|null>, "catalog": <frozen source|null>},
  "observed": {"db": <observed db source|null>,
               "catalog": <observed catalog source|null>}}``

* A ``frozen`` entry exists for -- and only for -- every configured source
  (``config.dbPath`` / ``config.catalogPath``) and records the startup freeze:
  ``{"metadata": {pathSha256, device, inode}, "contentSha256": <64 hex|null>}``.
  ``contentSha256`` is an OPTIONAL reader-supplied stable fingerprint; it never enters
  any identity digest.
* An ``observed`` entry exists for exactly the same sources and records the current
  trusted read-only observation:
  ``{"present": bool, "readable": bool, "metadata": <metadata|null>,
     "contentSha256": <64 hex|null>, "verified": bool}``
  where ``verified`` is the reader's own read-only structural check (sqlite schema /
  integrity or catalog schema). The catalog entry additionally carries
  ``"projectCollision": bool`` (a catalog project id collides with a real project).
* Consistency is enforced: ``present`` true requires usable metadata; ``present`` false
  requires ``readable``/``verified`` false, null metadata, null fingerprint and no
  collision; ``readable`` false forbids a fingerprint, ``verified`` true and a
  collision.

Classification precedence (deterministic; first match wins)
----------------------------------------------------------
``source_missing`` (any configured source absent) > ``source_unreadable`` (present but
not readable) > ``source_replaced`` (observed ``{pathSha256, device, inode}`` differs
from the frozen record) > ``database_invalid`` (db structural check failed) >
``catalog_invalid`` (catalog structural check failed) > ``source_changed`` (db
fingerprint supplied on both sides and different) > ``catalog_changed`` (catalog
fingerprint supplied on both sides and different) > ``project_collision`` > ``ok``.
Configured sources are always examined in ``db`` then ``catalog`` order.

Honest limitations (not claimed as covered)
-------------------------------------------
* No whole-database content hashing: DB content updates keep the same file identity and
  stay ``ok`` by design. An in-place overwrite that preserves device/inode while no
  fingerprint is supplied is NOT detected.
* A content change is reported only when the trusted reader supplied a fingerprint for
  both the frozen and the observed side; otherwise no content claim is made.
* ``ready`` reflects this supplied snapshot only. This is not a live probe, not a
  ``/healthz`` implementation, not a launchd/App/service change and not authentication.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "compute_config_identity",
    "compute_data_source_identity",
    "validate_health_payload",
    "evaluate_health_payload",
    "validate_source_snapshot",
    "classify_source_drift",
    "build_health_payload",
    "serialize_health_payload",
    "evaluate_source_snapshot",
]

# --------------------------------------------------------------------------- #
# Frozen G2-A protocol constants (unchanged)
# --------------------------------------------------------------------------- #
CONFIG_IDENTITY_SCHEMA = "d4-config-identity.v1"
DATA_SOURCE_IDENTITY_SCHEMA = "d4-data-source-identity.v1"
HEALTH_SCHEMA = "d4-board-health.v1"
SERVICE_NAME = "d4-board"
APP_CONFIG_SCHEMA = "d4-board-app-config.v1"
ALLOWED_HOST = "127.0.0.1"
ALLOWED_PYTHON_PATH = "/usr/bin/python3"
BOARD_SCRIPT_BASENAME = "d4_board.py"
LAUNCH_AGENT_LABEL = "com.conductor.d4.board"
PORT_MIN = 1024
PORT_MAX = 65535
AUTH_NONE = "none"

# Exact nine G1 serialized-config field names.
CONFIG_FIELDS = (
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

# Exact trusted-reader metadata fields.
METADATA_FIELDS = ("pathSha256", "device", "inode")

# Exact health payload fields.
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

# Exact nested data-source fields.
DATA_SOURCE_FIELDS = ("kind", "present", "identity")

# "none" is forbidden: the G1 config guarantees at least one source.
DATASOURCE_KINDS = ("sqlite", "catalog", "sqlite+catalog")

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

TEMPORARY_PREFIXES = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")

_LOWER_HEX = frozenset("0123456789abcdef")

# --------------------------------------------------------------------------- #
# Reader-layer schema constants (candidate additions)
# --------------------------------------------------------------------------- #
SNAPSHOT_SCHEMA = "d4-source-snapshot.v1"
SNAPSHOT_FIELDS = ("schemaVersion", "boardSha256", "config", "frozen", "observed")
SOURCE_RECORD_FIELDS = ("db", "catalog")
FROZEN_SOURCE_FIELDS = ("metadata", "contentSha256")
OBSERVED_DB_FIELDS = ("present", "readable", "metadata", "contentSha256", "verified")
OBSERVED_CATALOG_FIELDS = (
    "present",
    "readable",
    "metadata",
    "contentSha256",
    "verified",
    "projectCollision",
)

# Documented classification order; index 0 is the highest-precedence failure.
DRIFT_PRECEDENCE = (
    "source_missing",
    "source_unreadable",
    "source_replaced",
    "database_invalid",
    "catalog_invalid",
    "source_changed",
    "catalog_changed",
    "project_collision",
    "ok",
)


# --------------------------------------------------------------------------- #
# Canonicalization (frozen rule, unchanged)
# --------------------------------------------------------------------------- #
def _canonical_bytes(obj: Any) -> bytes:
    """Normative canonical bytes: compact, sorted, UTF-8, no trailing newline."""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _canonical_digest(obj: Any) -> str:
    return hashlib.sha256(_canonical_bytes(obj)).hexdigest()


# --------------------------------------------------------------------------- #
# Scalar / structural validators (generic diagnostics only)
# --------------------------------------------------------------------------- #
def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _require_object(value: Any) -> Mapping:
    if _is_bool(value) or isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Mapping):
        raise ValueError("invalid object")
    return value


def _require_exact_keys(mapping: Mapping, expected: Tuple[str, ...]) -> None:
    if set(mapping.keys()) != set(expected):
        raise ValueError("invalid object fields")


def _require_text(value: Any, field: str) -> str:
    if _is_bool(value) or not isinstance(value, str):
        raise ValueError("invalid %s" % field)
    if value == "":
        raise ValueError("invalid %s" % field)
    for char in value:
        if char == "\x00" or unicodedata.category(char).startswith("C"):
            raise ValueError("invalid %s" % field)
    return value


def _require_digest(value: Any, field: str) -> str:
    if _is_bool(value) or not isinstance(value, str):
        raise ValueError("invalid %s" % field)
    if len(value) != 64 or any(char not in _LOWER_HEX for char in value):
        raise ValueError("invalid %s" % field)
    return value


def _optional_digest(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    return _require_digest(value, field)


def _require_bool(value: Any, field: str) -> bool:
    if not _is_bool(value):
        raise ValueError("invalid %s" % field)
    return value


def _require_canonical_path(value: Any, field: str) -> str:
    text = _require_text(value, field)
    if not text.startswith("/"):
        raise ValueError("invalid %s" % field)
    for part in text.split("/")[1:]:
        if part in ("", ".", ".."):
            raise ValueError("invalid %s" % field)
    for prefix in TEMPORARY_PREFIXES:
        if text == prefix or text.startswith(prefix + "/"):
            raise ValueError("invalid %s" % field)
    return text


def _require_port(value: Any) -> int:
    if _is_bool(value) or not isinstance(value, int):
        raise ValueError("invalid port")
    if value < PORT_MIN or value > PORT_MAX:
        raise ValueError("invalid port")
    return value


# --------------------------------------------------------------------------- #
# Config identity (frozen semantics, unchanged)
# --------------------------------------------------------------------------- #
def _validate_config_fields(config: Any) -> Dict[str, Any]:
    """Syntax-only validation of the exact nine G1 config fields.

    Returns a fresh dict containing exactly the nine validated fields. Does NOT
    resolve, stat or perform any filesystem/existence/symlink/approved-root checks.
    """
    obj = _require_object(config)
    _require_exact_keys(obj, CONFIG_FIELDS)

    schema_version = obj["schemaVersion"]
    if schema_version != APP_CONFIG_SCHEMA:
        raise ValueError("invalid schemaVersion")

    host = obj["host"]
    if host != ALLOWED_HOST:
        raise ValueError("invalid host")

    port = _require_port(obj["port"])

    db_path = obj["dbPath"]
    if db_path is not None:
        db_path = _require_canonical_path(db_path, "dbPath")

    catalog_path = obj["catalogPath"]
    if catalog_path is not None:
        catalog_path = _require_canonical_path(catalog_path, "catalogPath")

    if db_path is None and catalog_path is None:
        raise ValueError("invalid data source selection")

    python_path = obj["pythonPath"]
    if python_path != ALLOWED_PYTHON_PATH:
        raise ValueError("invalid pythonPath")

    board_script = _require_canonical_path(obj["boardScript"], "boardScript")
    if board_script.rsplit("/", 1)[-1] != BOARD_SCRIPT_BASENAME:
        raise ValueError("invalid boardScript")

    runtime_dir = _require_canonical_path(obj["runtimeDir"], "runtimeDir")

    label = obj["launchAgentLabel"]
    if label != LAUNCH_AGENT_LABEL:
        raise ValueError("invalid launchAgentLabel")

    return {
        "schemaVersion": APP_CONFIG_SCHEMA,
        "host": ALLOWED_HOST,
        "port": port,
        "dbPath": db_path,
        "catalogPath": catalog_path,
        "pythonPath": ALLOWED_PYTHON_PATH,
        "boardScript": board_script,
        "runtimeDir": runtime_dir,
        "launchAgentLabel": LAUNCH_AGENT_LABEL,
    }


def compute_config_identity(config: Any, *, board_sha256: Any) -> str:
    """Return the canonical config-instance identity digest.

    Canonical object is EXACTLY::

        {"schemaVersion": "d4-config-identity.v1",
         "boardSha256": board_sha256,
         "config": <fresh exact nine G1 fields>}

    Raises ``ValueError`` on invalid config syntax or an invalid board digest.
    """
    digest_sha = _require_digest(board_sha256, "boardSha256")
    fields = _validate_config_fields(config)
    canonical = {
        "schemaVersion": CONFIG_IDENTITY_SCHEMA,
        "boardSha256": digest_sha,
        "config": fields,
    }
    return _canonical_digest(canonical)


# --------------------------------------------------------------------------- #
# Data-source identity (frozen semantics, unchanged)
# --------------------------------------------------------------------------- #
def _validate_metadata(meta: Any, field: str) -> Optional[Dict[str, Any]]:
    if meta is None:
        return None
    obj = _require_object(meta)
    _require_exact_keys(obj, METADATA_FIELDS)
    path_sha = _require_digest(obj["pathSha256"], "pathSha256")
    device = obj["device"]
    if _is_bool(device) or not isinstance(device, int) or device < 0:
        raise ValueError("invalid device")
    inode = obj["inode"]
    if _is_bool(inode) or not isinstance(inode, int) or inode <= 0:
        raise ValueError("invalid inode")
    return {"pathSha256": path_sha, "device": device, "inode": inode}


def compute_data_source_identity(*, db: Any = None, catalog: Any = None) -> str:
    """Return the canonical data-source identity digest from supplied metadata.

    Canonical object is EXACTLY::

        {"schemaVersion": "d4-data-source-identity.v1",
         "db": <fresh metadata or null>,
         "catalog": <fresh metadata or null>}

    At least one metadata must be present. No ``stat``/read/open of any source is
    performed; mutable DB contents/size/mtime are deliberately excluded.
    """
    db_meta = _validate_metadata(db, "db")
    catalog_meta = _validate_metadata(catalog, "catalog")
    if db_meta is None and catalog_meta is None:
        raise ValueError("at least one data source required")
    canonical = {
        "schemaVersion": DATA_SOURCE_IDENTITY_SCHEMA,
        "db": db_meta,
        "catalog": catalog_meta,
    }
    return _canonical_digest(canonical)


# --------------------------------------------------------------------------- #
# Health payload shape validation (frozen semantics, unchanged)
# --------------------------------------------------------------------------- #
def validate_health_payload(payload: Any) -> Dict[str, Any]:
    """Validate an untrusted health payload and return a fresh, isolated copy.

    Raises ``ValueError`` on any missing/extra key, wrong type, non-object, nested
    extra key or inconsistent readiness state. The returned dict is a new outer dict
    with a new nested ``dataSource`` dict, so it cannot mutate the input.
    """
    obj = _require_object(payload)
    _require_exact_keys(obj, HEALTH_FIELDS)

    schema_version = obj["schemaVersion"]
    if schema_version != HEALTH_SCHEMA:
        raise ValueError("invalid schemaVersion")

    service = obj["service"]
    if service != SERVICE_NAME:
        raise ValueError("invalid service")

    board_sha = _require_digest(obj["boardSha256"], "boardSha256")
    config_identity = _require_digest(obj["configIdentity"], "configIdentity")

    data_source = _require_object(obj["dataSource"])
    _require_exact_keys(data_source, DATA_SOURCE_FIELDS)

    kind = data_source["kind"]
    if kind not in DATASOURCE_KINDS:
        raise ValueError("invalid kind")

    present = data_source["present"]
    if not _is_bool(present):
        raise ValueError("invalid present")

    identity = data_source["identity"]
    if present:
        identity = _require_digest(identity, "identity")
    elif identity is not None:
        raise ValueError("invalid identity")

    ready = obj["ready"]
    if not _is_bool(ready):
        raise ValueError("invalid ready")

    reason = obj["reason"]
    if reason not in REASONS:
        raise ValueError("invalid reason")

    auth = obj["auth"]
    if auth != AUTH_NONE:
        raise ValueError("invalid auth")

    # Readiness <-> reason <-> presence consistency.
    if ready:
        if reason != "ok":
            raise ValueError("inconsistent readiness")
        if not present:
            raise ValueError("inconsistent readiness")
    else:
        if reason == "ok":
            raise ValueError("inconsistent readiness")
        if not present and reason != "source_missing":
            raise ValueError("inconsistent readiness")

    return {
        "schemaVersion": HEALTH_SCHEMA,
        "service": SERVICE_NAME,
        "boardSha256": board_sha,
        "configIdentity": config_identity,
        "dataSource": {"kind": kind, "present": present, "identity": identity},
        "ready": ready,
        "reason": reason,
        "auth": AUTH_NONE,
    }


# --------------------------------------------------------------------------- #
# Expected-vs-reported evaluation (frozen semantics, unchanged)
# --------------------------------------------------------------------------- #
def evaluate_health_payload(
    payload: Any,
    *,
    expected_board_sha256: Any,
    expected_config_identity: Any,
    expected_data_source_identity: Any,
) -> Tuple[bool, str]:
    """Compare an untrusted payload against trusted expected digests (fail-closed).

    Trusted expected digests are validated first (bad expected -> ``ValueError``).
    Then the payload is validated; an invalid payload returns
    ``(False, "invalid_payload")``. Board, config and data-source digests are compared
    in that order. A matched-but-not-ready payload returns ``(False, payload.reason)``;
    otherwise ``(True, "ok")``. This is misconfiguration detection, NOT authentication.
    """
    exp_board = _require_digest(expected_board_sha256, "expected_board_sha256")
    exp_config = _require_digest(expected_config_identity, "expected_config_identity")
    exp_data = _require_digest(expected_data_source_identity, "expected_data_source_identity")

    try:
        validated = validate_health_payload(payload)
    except ValueError:
        return (False, "invalid_payload")

    if validated["boardSha256"] != exp_board:
        return (False, "board_mismatch")
    if validated["configIdentity"] != exp_config:
        return (False, "config_mismatch")
    if validated["dataSource"]["identity"] != exp_data:
        return (False, "data_source_mismatch")
    if not validated["ready"]:
        return (False, validated["reason"])
    return (True, "ok")


# --------------------------------------------------------------------------- #
# Reader layer: trusted snapshot validation
# --------------------------------------------------------------------------- #
def _validate_frozen_source(value: Any, field: str) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    obj = _require_object(value)
    _require_exact_keys(obj, FROZEN_SOURCE_FIELDS)
    metadata = _validate_metadata(obj["metadata"], field)
    if metadata is None:
        raise ValueError("invalid %s" % field)
    content_sha = _optional_digest(obj["contentSha256"], "contentSha256")
    return {"metadata": metadata, "contentSha256": content_sha}


def _validate_observed_source(value: Any, field: str, *, catalog: bool) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    obj = _require_object(value)
    _require_exact_keys(obj, OBSERVED_CATALOG_FIELDS if catalog else OBSERVED_DB_FIELDS)

    present = _require_bool(obj["present"], "present")
    readable = _require_bool(obj["readable"], "readable")
    verified = _require_bool(obj["verified"], "verified")
    metadata = _validate_metadata(obj["metadata"], field)
    content_sha = _optional_digest(obj["contentSha256"], "contentSha256")
    collision = _require_bool(obj["projectCollision"], "projectCollision") if catalog else False

    if present:
        # File identity metadata is required whenever the source is reported present.
        if metadata is None:
            raise ValueError("invalid %s" % field)
    elif readable or verified or metadata is not None or content_sha is not None or collision:
        raise ValueError("invalid %s" % field)

    if not readable and (verified or content_sha is not None):
        raise ValueError("invalid %s" % field)

    if collision and not readable:
        raise ValueError("invalid %s" % field)

    result = {
        "present": present,
        "readable": readable,
        "metadata": metadata,
        "contentSha256": content_sha,
        "verified": verified,
    }
    if catalog:
        result["projectCollision"] = collision
    return result


def _validate_snapshot_object(snapshot: Any) -> Dict[str, Any]:
    obj = _require_object(snapshot)
    _require_exact_keys(obj, SNAPSHOT_FIELDS)

    if obj["schemaVersion"] != SNAPSHOT_SCHEMA:
        raise ValueError("invalid schemaVersion")

    board_sha = _require_digest(obj["boardSha256"], "boardSha256")
    config = _validate_config_fields(obj["config"])

    frozen = _require_object(obj["frozen"])
    _require_exact_keys(frozen, SOURCE_RECORD_FIELDS)
    observed = _require_object(obj["observed"])
    _require_exact_keys(observed, SOURCE_RECORD_FIELDS)

    frozen_db = _validate_frozen_source(frozen["db"], "db")
    frozen_catalog = _validate_frozen_source(frozen["catalog"], "catalog")
    observed_db = _validate_observed_source(observed["db"], "db", catalog=False)
    observed_catalog = _validate_observed_source(observed["catalog"], "catalog", catalog=True)

    db_configured = config["dbPath"] is not None
    catalog_configured = config["catalogPath"] is not None

    # Exactly the configured sources must be recorded, on both sides.
    if db_configured != (frozen_db is not None) or db_configured != (observed_db is not None):
        raise ValueError("invalid snapshot sources")
    if catalog_configured != (frozen_catalog is not None) or catalog_configured != (observed_catalog is not None):
        raise ValueError("invalid snapshot sources")

    return {
        "schemaVersion": SNAPSHOT_SCHEMA,
        "boardSha256": board_sha,
        "config": config,
        "frozen": {"db": frozen_db, "catalog": frozen_catalog},
        "observed": {"db": observed_db, "catalog": observed_catalog},
    }


def validate_source_snapshot(snapshot: Any) -> Dict[str, Any]:
    """Validate one supplied trusted snapshot and return a fresh, isolated copy.

    Raises ``ValueError`` (generic diagnostic only) for a non-object, a missing/extra
    key at any level, a bad schema version, an invalid board digest, an invalid nine
    field config, an invalid source metadata record, a source set that does not match
    the configured ``dbPath``/``catalogPath`` selection, or an internally inconsistent
    observation. Performs no I/O and never returns a path, token or raw input data.
    """
    return _validate_snapshot_object(snapshot)


# --------------------------------------------------------------------------- #
# Reader layer: drift classification and bounded payload construction
# --------------------------------------------------------------------------- #
def _configured_kind(config: Mapping) -> str:
    has_db = config["dbPath"] is not None
    has_catalog = config["catalogPath"] is not None
    if has_db and has_catalog:
        return "sqlite+catalog"
    if has_db:
        return "sqlite"
    return "catalog"


def _configured_pairs(validated: Mapping) -> Tuple[Tuple[str, Any, Any, str], ...]:
    """Return (name, frozen, observed, content-change-reason) in db then catalog order."""
    config = validated["config"]
    frozen = validated["frozen"]
    observed = validated["observed"]
    pairs = []
    if config["dbPath"] is not None:
        pairs.append(("db", frozen["db"], observed["db"], "source_changed"))
    if config["catalogPath"] is not None:
        pairs.append(("catalog", frozen["catalog"], observed["catalog"], "catalog_changed"))
    return tuple(pairs)


def _classify_validated_snapshot(validated: Mapping) -> str:
    config = validated["config"]
    observed = validated["observed"]
    pairs = _configured_pairs(validated)

    # 1. presence
    for _name, _frozen, entry, _reason in pairs:
        if entry["present"] is False:
            return "source_missing"

    # 2. readability
    for _name, _frozen, entry, _reason in pairs:
        if entry["readable"] is False:
            return "source_unreadable"

    # 3. file-identity replacement (path digest / device / inode)
    for _name, frozen, entry, _reason in pairs:
        if entry["metadata"] != frozen["metadata"]:
            return "source_replaced"

    # 4. read-only structural checks
    if config["dbPath"] is not None and observed["db"]["verified"] is False:
        return "database_invalid"
    if config["catalogPath"] is not None and observed["catalog"]["verified"] is False:
        return "catalog_invalid"

    # 5. content-fingerprint change (only when both sides supplied a fingerprint)
    for _name, frozen, entry, changed_reason in pairs:
        frozen_sha = frozen["contentSha256"]
        observed_sha = entry["contentSha256"]
        if frozen_sha is not None and observed_sha is not None and frozen_sha != observed_sha:
            return changed_reason

    # 6. catalog project identifiers colliding with real projects
    if config["catalogPath"] is not None and observed["catalog"]["projectCollision"] is True:
        return "project_collision"

    return "ok"


def classify_source_drift(snapshot: Any) -> str:
    """Return exactly one frozen reason code for the supplied trusted snapshot.

    The snapshot is validated first (malformed input raises ``ValueError``). Otherwise
    exactly one code is produced using the documented, deterministic precedence:
    ``source_missing``, ``source_unreadable``, ``source_replaced``, ``database_invalid``,
    ``catalog_invalid``, ``source_changed``, ``catalog_changed``, ``project_collision``,
    ``ok``. ``ok`` means "the supplied observation shows no missing, unreadable,
    replaced, structurally invalid, fingerprint-changed or colliding source" -- it is
    not a live probe and not a claim about the real filesystem.
    """
    return _classify_validated_snapshot(_validate_snapshot_object(snapshot))


def build_health_payload(snapshot: Any) -> Dict[str, Any]:
    """Build the bounded frozen ``d4-board-health.v1`` payload for a trusted snapshot.

    ``boardSha256`` and ``configIdentity`` come from the trusted snapshot inputs;
    ``dataSource.identity`` is the digest of the *observed* file-identity metadata (or
    ``null`` when a configured source is missing). The constructed payload is
    re-validated with ``validate_health_payload`` before being returned, so the output
    can never contain an unknown field, a path, a token or free-form text. Raises
    ``ValueError`` for a malformed snapshot.
    """
    validated = _validate_snapshot_object(snapshot)
    reason = _classify_validated_snapshot(validated)

    config = validated["config"]
    observed = validated["observed"]
    board_sha = validated["boardSha256"]

    kind = _configured_kind(config)
    present = reason != "source_missing"

    if present:
        observed_db = observed["db"]
        observed_catalog = observed["catalog"]
        identity = compute_data_source_identity(
            db=observed_db["metadata"] if observed_db is not None else None,
            catalog=observed_catalog["metadata"] if observed_catalog is not None else None,
        )
    else:
        identity = None

    payload = {
        "schemaVersion": HEALTH_SCHEMA,
        "service": SERVICE_NAME,
        "boardSha256": board_sha,
        "configIdentity": compute_config_identity(config, board_sha256=board_sha),
        "dataSource": {"kind": kind, "present": present, "identity": identity},
        "ready": reason == "ok",
        "reason": reason,
        "auth": AUTH_NONE,
    }
    return validate_health_payload(payload)


def serialize_health_payload(payload: Any) -> bytes:
    """Validate a health payload and return its canonical, bounded JSON bytes.

    Canonical bytes use the frozen rule (``ensure_ascii=False``, ``sort_keys=True``,
    compact separators, UTF-8, no trailing newline). The output is bounded because the
    frozen validator admits exactly eight keys whose values are fixed-width digests,
    booleans, fixed literals or enum codes: no path, token, free-form message or raw
    supplied data can survive validation. Raises ``ValueError`` for an invalid payload.
    """
    return _canonical_bytes(validate_health_payload(payload))


def evaluate_source_snapshot(
    snapshot: Any,
    *,
    expected_board_sha256: Any,
    expected_config_identity: Any,
    expected_data_source_identity: Any,
) -> Tuple[bool, str]:
    """Compare a trusted snapshot against trusted expected digests (fail-closed).

    Trusted expected digests are validated first (bad expected -> ``ValueError``). An
    invalid snapshot returns ``(False, "invalid_payload")``. Otherwise the payload built
    from the snapshot is evaluated with the frozen order (board, then config, then data
    source, then the readiness reason). This detects a wrong configured instance or a
    replaced/changed data source; it is misconfiguration detection, NOT authentication,
    and it never launches or contacts a service.
    """
    exp_board = _require_digest(expected_board_sha256, "expected_board_sha256")
    exp_config = _require_digest(expected_config_identity, "expected_config_identity")
    exp_data = _require_digest(expected_data_source_identity, "expected_data_source_identity")

    try:
        payload = build_health_payload(snapshot)
    except ValueError:
        return (False, "invalid_payload")

    return evaluate_health_payload(
        payload,
        expected_board_sha256=exp_board,
        expected_config_identity=exp_config,
        expected_data_source_identity=exp_data,
    )
