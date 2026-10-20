"""Independent unittest suite for candidate/scripts/d4_health_reader.py (G2-B reader).

Pure synthetic Python objects only: no files, no database, no sockets, no subprocesses,
no environment variables and no CLI. Expected digests and expected health payloads are
rebuilt here from an independently stated canonical formula (json.dumps with
ensure_ascii=False / sort_keys=True / compact separators + sha256) and from literal
frozen enum/field names, never by asking the module under test to produce its own
expected value.

The candidate module is imported from the exact relative file path
``<tests parent>/scripts/d4_health_reader.py`` via an importlib file spec; there is no
directory scan, no environment override and no import of the frozen input modules.
"""

import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

_CANDIDATE_MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "d4_health_reader.py"
_spec = importlib.util.spec_from_file_location("d4_health_reader", _CANDIDATE_MODULE_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive
    raise ImportError("cannot load candidate module")
d4 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d4)


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

HEX_A = "a" * 64
HEX_B = "b" * 64
HEX_C = "c" * 64
HEX_D = "d" * 64
HEX_E = "e" * 64
BOARD_SHA = "9" * 64
LOWER_HEX = "0123456789abcdef"


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


def independent_config_identity(config, board_sha256):
    fields = {name: config[name] for name in CONFIG_FIELD_NAMES}
    obj = {
        "schemaVersion": "d4-config-identity.v1",
        "boardSha256": board_sha256,
        "config": fields,
    }
    return independent_digest(obj)


def independent_data_source_identity(db, catalog):
    def clean(meta):
        if meta is None:
            return None
        return {
            "pathSha256": meta["pathSha256"],
            "device": meta["device"],
            "inode": meta["inode"],
        }

    obj = {
        "schemaVersion": "d4-data-source-identity.v1",
        "db": clean(db),
        "catalog": clean(catalog),
    }
    return independent_digest(obj)


def independent_expected_payload(snapshot, *, kind, present, ready, reason):
    board_sha = snapshot["boardSha256"]
    observed_db = snapshot["observed"]["db"]
    observed_catalog = snapshot["observed"]["catalog"]
    identity = None
    if present:
        identity = independent_data_source_identity(
            observed_db["metadata"] if observed_db is not None else None,
            observed_catalog["metadata"] if observed_catalog is not None else None,
        )
    return {
        "schemaVersion": "d4-board-health.v1",
        "service": "d4-board",
        "boardSha256": board_sha,
        "configIdentity": independent_config_identity(snapshot["config"], board_sha),
        "dataSource": {"kind": kind, "present": present, "identity": identity},
        "ready": ready,
        "reason": reason,
        "auth": "none",
    }


# --------------------------------------------------------------------------- #
# Synthetic fixtures (plain dicts; all digest/bool/int values literal here)
# --------------------------------------------------------------------------- #
def valid_config(**overrides):
    config = {
        "schemaVersion": "d4-board-app-config.v1",
        "host": "127.0.0.1",
        "port": 8787,
        "dbPath": "/opt/d4/data/board.sqlite3",
        "catalogPath": None,
        "pythonPath": "/usr/bin/python3",
        "boardScript": "/opt/d4/app/d4_board.py",
        "runtimeDir": "/opt/d4/runtime",
        "launchAgentLabel": "com.conductor.d4.board",
    }
    config.update(overrides)
    return config


DB_ONLY_CONFIG = valid_config()
CATALOG_ONLY_CONFIG = valid_config(dbPath=None, catalogPath="/opt/d4/data/catalog.json")
BOTH_CONFIG = valid_config(catalogPath="/opt/d4/data/catalog.json")

DB_META = {"pathSha256": HEX_A, "device": 7, "inode": 42}
CAT_META = {"pathSha256": HEX_B, "device": 3, "inode": 9}


def metadata(path_sha=HEX_A, device=7, inode=42):
    return {"pathSha256": path_sha, "device": device, "inode": inode}


def dropped(mapping, *keys):
    return {key: value for key, value in mapping.items() if key not in keys}


def frozen_source(meta=None, content_sha256=None):
    return {
        "metadata": dict(meta) if meta is not None else None,
        "contentSha256": content_sha256,
    }


def observed_db(meta, *, present=True, readable=True, content_sha256=None, verified=True):
    return {
        "present": present,
        "readable": readable,
        "metadata": dict(meta) if meta is not None else None,
        "contentSha256": content_sha256,
        "verified": verified,
    }


def observed_catalog(
    meta,
    *,
    present=True,
    readable=True,
    content_sha256=None,
    verified=True,
    project_collision=False,
):
    return {
        "present": present,
        "readable": readable,
        "metadata": dict(meta) if meta is not None else None,
        "contentSha256": content_sha256,
        "verified": verified,
        "projectCollision": project_collision,
    }


def make_snapshot(
    config,
    *,
    board_sha256=BOARD_SHA,
    frozen_db=None,
    frozen_catalog=None,
    db_entry=None,
    catalog_entry=None,
):
    return {
        "schemaVersion": "d4-source-snapshot.v1",
        "boardSha256": board_sha256,
        "config": config,
        "frozen": {"db": frozen_db, "catalog": frozen_catalog},
        "observed": {"db": db_entry, "catalog": catalog_entry},
    }


def db_only_snapshot(*, db_entry=None, frozen_db=None, board_sha256=BOARD_SHA):
    return make_snapshot(
        DB_ONLY_CONFIG,
        board_sha256=board_sha256,
        frozen_db=frozen_source(DB_META) if frozen_db is None else frozen_db,
        db_entry=observed_db(DB_META) if db_entry is None else db_entry,
    )


def catalog_only_snapshot(*, catalog_entry=None, frozen_catalog=None, board_sha256=BOARD_SHA):
    return make_snapshot(
        CATALOG_ONLY_CONFIG,
        board_sha256=board_sha256,
        frozen_catalog=frozen_source(CAT_META) if frozen_catalog is None else frozen_catalog,
        catalog_entry=observed_catalog(CAT_META) if catalog_entry is None else catalog_entry,
    )


def both_snapshot(
    *,
    db_entry=None,
    catalog_entry=None,
    frozen_db=None,
    frozen_catalog=None,
    board_sha256=BOARD_SHA,
):
    return make_snapshot(
        BOTH_CONFIG,
        board_sha256=board_sha256,
        frozen_db=frozen_source(DB_META) if frozen_db is None else frozen_db,
        frozen_catalog=frozen_source(CAT_META) if frozen_catalog is None else frozen_catalog,
        db_entry=observed_db(DB_META) if db_entry is None else db_entry,
        catalog_entry=observed_catalog(CAT_META) if catalog_entry is None else catalog_entry,
    )


def absent_db(**overrides):
    entry = observed_db(None, present=False, readable=False, verified=False)
    entry.update(overrides)
    return entry


def absent_catalog(**overrides):
    entry = observed_catalog(None, present=False, readable=False, verified=False)
    entry.update(overrides)
    return entry


def malformed_snapshots():
    base = db_only_snapshot()
    both = both_snapshot()
    catalog_base = catalog_only_snapshot()
    cases = {}

    cases["not object"] = [1, 2, 3]
    cases["string"] = "snapshot"
    cases["none"] = None
    cases["bool"] = True
    cases["missing key"] = dropped(base, "observed")
    cases["extra key"] = dict(base, extra=1)
    cases["bad schema"] = dict(base, schemaVersion="d4-source-snapshot.v2")
    cases["board sha null"] = dict(base, boardSha256=None)
    cases["board sha bool"] = dict(base, boardSha256=True)
    cases["board sha uppercase"] = dict(base, boardSha256="A" * 64)
    cases["board sha short"] = dict(base, boardSha256="1" * 63)
    cases["board sha non hex"] = dict(base, boardSha256="z" * 64)

    cases["config missing field"] = dict(base, config=dropped(valid_config(), "port"))
    cases["config extra field"] = dict(base, config=dict(valid_config(), extra=1))
    cases["config no source"] = dict(base, config=valid_config(dbPath=None, catalogPath=None))
    cases["config relative db"] = dict(base, config=valid_config(dbPath="data/board.sqlite3"))
    cases["config not object"] = dict(base, config="config")

    cases["frozen not object"] = dict(base, frozen="x")
    cases["frozen missing key"] = dict(base, frozen={"db": frozen_source(DB_META)})
    cases["frozen extra key"] = dict(
        base, frozen={"db": frozen_source(DB_META), "catalog": None, "extra": 1}
    )
    cases["frozen entry extra key"] = dict(
        base, frozen={"db": dict(frozen_source(DB_META), extra=1), "catalog": None}
    )
    cases["frozen entry missing key"] = dict(
        base, frozen={"db": {"metadata": dict(DB_META)}, "catalog": None}
    )
    cases["frozen metadata null"] = dict(base, frozen={"db": frozen_source(None), "catalog": None})
    cases["frozen metadata noisy key"] = dict(
        base,
        frozen={"db": {"metadata": dict(DB_META, size=1), "contentSha256": None}, "catalog": None},
    )
    cases["frozen metadata bool device"] = dict(
        base, frozen={"db": frozen_source(metadata(HEX_A, True, 42)), "catalog": None}
    )
    cases["frozen metadata zero inode"] = dict(
        base, frozen={"db": frozen_source(metadata(HEX_A, 7, 0)), "catalog": None}
    )
    cases["frozen metadata float inode"] = dict(
        base, frozen={"db": frozen_source(metadata(HEX_A, 7, 42.0)), "catalog": None}
    )
    cases["frozen metadata uppercase path sha"] = dict(
        base, frozen={"db": frozen_source(metadata("A" * 64, 7, 42)), "catalog": None}
    )
    cases["frozen fingerprint uppercase"] = dict(
        base, frozen={"db": frozen_source(DB_META, "A" * 64), "catalog": None}
    )
    cases["frozen fingerprint int"] = dict(
        base, frozen={"db": frozen_source(DB_META, 1), "catalog": None}
    )

    cases["observed not object"] = dict(base, observed=[1])
    cases["observed missing key"] = dict(base, observed={"db": observed_db(DB_META)})
    cases["observed extra key"] = dict(
        base, observed={"db": observed_db(DB_META), "catalog": None, "extra": True}
    )
    cases["observed entry extra key"] = dict(
        base,
        observed={"db": dict(observed_db(DB_META), projectCollision=False), "catalog": None},
    )
    cases["observed entry missing key"] = dict(
        base, observed={"db": dropped(observed_db(DB_META), "verified"), "catalog": None}
    )
    cases["metadata string"] = dict(
        base,
        observed={"db": dict(observed_db(DB_META), metadata="not-a-mapping"), "catalog": None},
    )
    cases["metadata uppercase path sha"] = dict(
        base, observed={"db": observed_db(metadata("A" * 64, 7, 42)), "catalog": None}
    )

    cases["present int"] = dict(base, observed={"db": observed_db(DB_META, present=1), "catalog": None})
    cases["present zero"] = dict(
        base, observed={"db": observed_db(DB_META, present=0), "catalog": None}
    )
    cases["readable string"] = dict(
        base, observed={"db": observed_db(DB_META, readable="yes"), "catalog": None}
    )
    cases["verified int"] = dict(
        base, observed={"db": observed_db(DB_META, verified=0), "catalog": None}
    )
    cases["present without metadata"] = dict(
        base, observed={"db": observed_db(None, present=True), "catalog": None}
    )
    cases["present false with readable"] = dict(
        base, observed={"db": observed_db(None, present=False, readable=True), "catalog": None}
    )
    cases["present false with metadata"] = dict(
        base, observed={"db": observed_db(DB_META, present=False, readable=False, verified=False), "catalog": None}
    )
    cases["present false with verified"] = dict(
        base, observed={"db": observed_db(None, present=False, readable=False, verified=True), "catalog": None}
    )
    cases["present false with fingerprint"] = dict(
        base,
        observed={
            "db": observed_db(None, present=False, readable=False, verified=False, content_sha256=HEX_C),
            "catalog": None,
        },
    )
    cases["unreadable with fingerprint"] = dict(
        base,
        observed={"db": observed_db(DB_META, readable=False, verified=False, content_sha256=HEX_C), "catalog": None},
    )
    cases["unreadable with verified"] = dict(
        base, observed={"db": observed_db(DB_META, readable=False, verified=True), "catalog": None}
    )

    cases["catalog collision not bool"] = dict(
        both,
        observed={
            "db": observed_db(DB_META),
            "catalog": observed_catalog(CAT_META, project_collision=1),
        },
    )
    cases["catalog collision while unreadable"] = dict(
        both,
        observed={
            "db": observed_db(DB_META),
            "catalog": observed_catalog(CAT_META, readable=False, verified=False, project_collision=True),
        },
    )
    cases["catalog collision while absent"] = dict(
        both,
        observed={
            "db": observed_db(DB_META),
            "catalog": observed_catalog(None, present=False, readable=False, verified=False, project_collision=True),
        },
    )
    cases["catalog entry missing key"] = dict(
        both,
        observed={
            "db": observed_db(DB_META),
            "catalog": dropped(observed_catalog(CAT_META), "projectCollision"),
        },
    )

    cases["db configured but frozen db null"] = dict(base, frozen={"db": None, "catalog": None})
    cases["db configured but observed db null"] = dict(base, observed={"db": None, "catalog": None})
    cases["db not configured but catalog recorded"] = dict(
        base,
        frozen={"db": frozen_source(DB_META), "catalog": frozen_source(CAT_META)},
        observed={"db": observed_db(DB_META), "catalog": observed_catalog(CAT_META)},
    )
    cases["catalog configured but observed null"] = dict(
        catalog_base, observed={"db": None, "catalog": None}
    )
    cases["catalog configured but frozen null"] = dict(
        catalog_base, frozen={"db": None, "catalog": None}
    )

    cases["health payload as snapshot"] = {
        "schemaVersion": "d4-board-health.v1",
        "service": "d4-board",
        "boardSha256": HEX_A,
        "configIdentity": HEX_B,
        "dataSource": {"kind": "sqlite", "present": True, "identity": HEX_C},
        "ready": True,
        "reason": "ok",
        "auth": "none",
    }
    return cases


# --------------------------------------------------------------------------- #
# Trusted snapshot validation
# --------------------------------------------------------------------------- #
class SnapshotValidationTests(unittest.TestCase):
    def test_db_only_snapshot_round_trips_as_fresh_copy(self):
        snapshot = db_only_snapshot()
        validated = d4.validate_source_snapshot(snapshot)
        self.assertEqual(validated, snapshot)
        self.assertIsNot(validated, snapshot)
        self.assertIsNot(validated["config"], snapshot["config"])
        self.assertIsNot(validated["frozen"], snapshot["frozen"])
        self.assertIsNot(validated["observed"], snapshot["observed"])
        self.assertIsNot(validated["frozen"]["db"], snapshot["frozen"]["db"])
        self.assertIsNot(validated["frozen"]["db"]["metadata"], snapshot["frozen"]["db"]["metadata"])
        self.assertIsNot(validated["observed"]["db"], snapshot["observed"]["db"])
        self.assertIsNot(validated["observed"]["db"]["metadata"], snapshot["observed"]["db"]["metadata"])

    def test_catalog_only_and_combined_snapshots_round_trip(self):
        for label, snapshot in (("catalog only", catalog_only_snapshot()), ("both", both_snapshot())):
            with self.subTest(case=label):
                self.assertEqual(d4.validate_source_snapshot(snapshot), snapshot)

    def test_mutating_validated_copy_does_not_touch_input(self):
        snapshot = both_snapshot()
        validated = d4.validate_source_snapshot(snapshot)
        validated["boardSha256"] = HEX_E
        validated["observed"]["db"]["present"] = False
        validated["observed"]["db"]["metadata"]["inode"] = 999
        validated["observed"]["catalog"]["projectCollision"] = True
        validated["frozen"]["catalog"]["contentSha256"] = HEX_E
        self.assertEqual(snapshot["boardSha256"], BOARD_SHA)
        self.assertIs(snapshot["observed"]["db"]["present"], True)
        self.assertEqual(snapshot["observed"]["db"]["metadata"]["inode"], 42)
        self.assertIs(snapshot["observed"]["catalog"]["projectCollision"], False)
        self.assertIsNone(snapshot["frozen"]["catalog"]["contentSha256"])

    def test_input_metadata_dicts_are_not_mutated(self):
        meta = metadata(HEX_A, 7, 42)
        before = json.loads(json.dumps(meta))
        snapshot = db_only_snapshot(frozen_db=frozen_source(meta), db_entry=observed_db(meta))
        d4.validate_source_snapshot(snapshot)
        d4.build_health_payload(snapshot)
        self.assertEqual(meta, before)

    def test_snapshot_key_order_does_not_matter(self):
        snapshot = both_snapshot()
        reordered = {
            "observed": snapshot["observed"],
            "config": snapshot["config"],
            "boardSha256": snapshot["boardSha256"],
            "schemaVersion": snapshot["schemaVersion"],
            "frozen": snapshot["frozen"],
        }
        self.assertEqual(
            d4.validate_source_snapshot(reordered), d4.validate_source_snapshot(snapshot)
        )
        self.assertEqual(d4.classify_source_drift(reordered), d4.classify_source_drift(snapshot))
        self.assertEqual(
            d4.build_health_payload(reordered), d4.build_health_payload(snapshot)
        )

    def test_malformed_snapshots_raise_value_error(self):
        cases = malformed_snapshots()
        self.assertGreaterEqual(len(cases), 45)
        for label, snapshot in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    d4.validate_source_snapshot(snapshot)

    def test_every_entry_point_rejects_malformed_snapshots(self):
        for label, snapshot in malformed_snapshots().items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    d4.classify_source_drift(snapshot)
                with self.assertRaises(ValueError):
                    d4.build_health_payload(snapshot)
                self.assertEqual(
                    d4.evaluate_source_snapshot(
                        snapshot,
                        expected_board_sha256=BOARD_SHA,
                        expected_config_identity=HEX_B,
                        expected_data_source_identity=HEX_C,
                    ),
                    (False, "invalid_payload"),
                )


# --------------------------------------------------------------------------- #
# Drift / replacement classification
# --------------------------------------------------------------------------- #
class DriftClassificationTests(unittest.TestCase):
    def test_no_drift_is_ok(self):
        for label, snapshot in (
            ("db only", db_only_snapshot()),
            ("catalog only", catalog_only_snapshot()),
            ("both", both_snapshot()),
        ):
            with self.subTest(case=label):
                self.assertEqual(d4.classify_source_drift(snapshot), "ok")

    def test_equal_fingerprints_on_both_sides_are_ok(self):
        snapshot = both_snapshot(
            frozen_db=frozen_source(DB_META, HEX_C),
            db_entry=observed_db(DB_META, content_sha256=HEX_C),
            frozen_catalog=frozen_source(CAT_META, HEX_D),
            catalog_entry=observed_catalog(CAT_META, content_sha256=HEX_D),
        )
        self.assertEqual(d4.classify_source_drift(snapshot), "ok")

    def test_missing_sources(self):
        cases = {
            "db only": db_only_snapshot(db_entry=absent_db()),
            "catalog only": catalog_only_snapshot(catalog_entry=absent_catalog()),
            "both: db missing": both_snapshot(db_entry=absent_db()),
            "both: catalog missing": both_snapshot(catalog_entry=absent_catalog()),
        }
        for label, snapshot in cases.items():
            with self.subTest(case=label):
                self.assertEqual(d4.classify_source_drift(snapshot), "source_missing")

    def test_unreadable_sources(self):
        cases = {
            "db": db_only_snapshot(db_entry=observed_db(DB_META, readable=False, verified=False)),
            "catalog": catalog_only_snapshot(
                catalog_entry=observed_catalog(CAT_META, readable=False, verified=False)
            ),
        }
        for label, snapshot in cases.items():
            with self.subTest(case=label):
                self.assertEqual(d4.classify_source_drift(snapshot), "source_unreadable")

    def test_replaced_sources(self):
        cases = {
            "db path digest": db_only_snapshot(db_entry=observed_db(metadata(HEX_D, 7, 42))),
            "db device": db_only_snapshot(db_entry=observed_db(metadata(HEX_A, 8, 42))),
            "db inode": db_only_snapshot(db_entry=observed_db(metadata(HEX_A, 7, 43))),
            "catalog inode": catalog_only_snapshot(
                catalog_entry=observed_catalog(metadata(HEX_B, 3, 10))
            ),
        }
        for label, snapshot in cases.items():
            with self.subTest(case=label):
                self.assertEqual(d4.classify_source_drift(snapshot), "source_replaced")

    def test_structurally_invalid_sources(self):
        self.assertEqual(
            d4.classify_source_drift(
                db_only_snapshot(db_entry=observed_db(DB_META, verified=False))
            ),
            "database_invalid",
        )
        self.assertEqual(
            d4.classify_source_drift(
                catalog_only_snapshot(
                    catalog_entry=observed_catalog(CAT_META, verified=False)
                )
            ),
            "catalog_invalid",
        )

    def test_content_fingerprint_change(self):
        self.assertEqual(
            d4.classify_source_drift(
                db_only_snapshot(
                    frozen_db=frozen_source(DB_META, HEX_C),
                    db_entry=observed_db(DB_META, content_sha256=HEX_D),
                )
            ),
            "source_changed",
        )
        self.assertEqual(
            d4.classify_source_drift(
                catalog_only_snapshot(
                    frozen_catalog=frozen_source(CAT_META, HEX_C),
                    catalog_entry=observed_catalog(CAT_META, content_sha256=HEX_D),
                )
            ),
            "catalog_changed",
        )

    def test_absent_or_partial_fingerprints_make_no_content_claim(self):
        # Honest limitation: a change is reported only when both sides carry a fingerprint.
        cases = {
            "frozen fingerprint absent": db_only_snapshot(
                frozen_db=frozen_source(DB_META, None),
                db_entry=observed_db(DB_META, content_sha256=HEX_D),
            ),
            "observed fingerprint absent": db_only_snapshot(
                frozen_db=frozen_source(DB_META, HEX_C),
                db_entry=observed_db(DB_META, content_sha256=None),
            ),
            "catalog frozen fingerprint absent": catalog_only_snapshot(
                frozen_catalog=frozen_source(CAT_META, None),
                catalog_entry=observed_catalog(CAT_META, content_sha256=HEX_D),
            ),
        }
        for label, snapshot in cases.items():
            with self.subTest(case=label):
                self.assertEqual(d4.classify_source_drift(snapshot), "ok")

    def test_project_collision(self):
        self.assertEqual(
            d4.classify_source_drift(
                catalog_only_snapshot(catalog_entry=observed_catalog(CAT_META, project_collision=True))
            ),
            "project_collision",
        )
        self.assertEqual(
            d4.classify_source_drift(
                both_snapshot(catalog_entry=observed_catalog(CAT_META, project_collision=True))
            ),
            "project_collision",
        )

    def test_precedence_is_deterministic(self):
        cases = {
            "missing beats unreadable": (
                both_snapshot(
                    db_entry=absent_db(),
                    catalog_entry=observed_catalog(CAT_META, readable=False, verified=False),
                ),
                "source_missing",
            ),
            "unreadable beats replaced": (
                both_snapshot(
                    db_entry=observed_db(DB_META, readable=False, verified=False),
                    catalog_entry=observed_catalog(metadata(HEX_D, 5, 55)),
                ),
                "source_unreadable",
            ),
            "replaced beats database_invalid": (
                db_only_snapshot(db_entry=observed_db(metadata(HEX_A, 7, 44), verified=False)),
                "source_replaced",
            ),
            "database_invalid beats source_changed": (
                db_only_snapshot(
                    frozen_db=frozen_source(DB_META, HEX_C),
                    db_entry=observed_db(DB_META, verified=False, content_sha256=HEX_D),
                ),
                "database_invalid",
            ),
            "catalog_invalid beats catalog_changed": (
                catalog_only_snapshot(
                    frozen_catalog=frozen_source(CAT_META, HEX_C),
                    catalog_entry=observed_catalog(CAT_META, verified=False, content_sha256=HEX_D),
                ),
                "catalog_invalid",
            ),
            "source_changed beats project_collision": (
                both_snapshot(
                    frozen_db=frozen_source(DB_META, HEX_C),
                    db_entry=observed_db(DB_META, content_sha256=HEX_D),
                    catalog_entry=observed_catalog(CAT_META, project_collision=True),
                ),
                "source_changed",
            ),
            "catalog_changed beats project_collision": (
                catalog_only_snapshot(
                    frozen_catalog=frozen_source(CAT_META, HEX_C),
                    catalog_entry=observed_catalog(
                        CAT_META, content_sha256=HEX_D, project_collision=True
                    ),
                ),
                "catalog_changed",
            ),
        }
        for label, (snapshot, expected) in cases.items():
            with self.subTest(case=label):
                self.assertEqual(d4.classify_source_drift(snapshot), expected)

    def test_classification_is_repeatable_and_order_independent(self):
        snapshot = both_snapshot(
            catalog_entry=observed_catalog(CAT_META, project_collision=True)
        )
        first = d4.classify_source_drift(snapshot)
        self.assertEqual(first, "project_collision")
        self.assertEqual(d4.classify_source_drift(snapshot), first)
        reordered = {key: snapshot[key] for key in reversed(list(snapshot.keys()))}
        self.assertEqual(d4.classify_source_drift(reordered), first)

    def test_classification_uses_only_the_frozen_enum(self):
        self.assertIn(d4.classify_source_drift(db_only_snapshot()), REASONS)
        self.assertEqual(d4.DRIFT_PRECEDENCE, (
            "source_missing",
            "source_unreadable",
            "source_replaced",
            "database_invalid",
            "catalog_invalid",
            "source_changed",
            "catalog_changed",
            "project_collision",
            "ok",
        ))
        self.assertEqual(
            set(d4.DRIFT_PRECEDENCE) - {"ok"},
            set(REASONS) - {"ok", "board_sha_unavailable"},
        )


# --------------------------------------------------------------------------- #
# Bounded health payload construction
# --------------------------------------------------------------------------- #
class HealthPayloadConstructionTests(unittest.TestCase):
    def assert_contract_payload(self, snapshot, *, kind, present, ready, reason):
        payload = d4.build_health_payload(snapshot)
        self.assertEqual(
            payload,
            independent_expected_payload(
                snapshot, kind=kind, present=present, ready=ready, reason=reason
            ),
        )
        self.assertEqual(set(payload.keys()), set(HEALTH_FIELDS))
        self.assertEqual(set(payload["dataSource"].keys()), set(DATA_SOURCE_FIELDS))
        self.assertEqual(payload, d4.validate_health_payload(payload))
        self.assertIsInstance(payload["ready"], bool)
        self.assertIsInstance(payload["dataSource"]["present"], bool)
        self.assertIn(payload["reason"], REASONS)
        self.assertEqual(payload["auth"], "none")
        if present:
            self.assertEqual(len(payload["dataSource"]["identity"]), 64)
            self.assertEqual(payload["dataSource"]["identity"], payload["dataSource"]["identity"].lower())
        else:
            self.assertIsNone(payload["dataSource"]["identity"])
        return payload

    def test_ready_db_only_payload(self):
        self.assert_contract_payload(
            db_only_snapshot(), kind="sqlite", present=True, ready=True, reason="ok"
        )

    def test_ready_catalog_only_payload(self):
        self.assert_contract_payload(
            catalog_only_snapshot(), kind="catalog", present=True, ready=True, reason="ok"
        )

    def test_ready_combined_payload(self):
        self.assert_contract_payload(
            both_snapshot(), kind="sqlite+catalog", present=True, ready=True, reason="ok"
        )

    def test_not_ready_payloads_are_still_contract_valid(self):
        cases = (
            ("db missing", db_only_snapshot(db_entry=absent_db()), "sqlite", False, "source_missing"),
            (
                "catalog missing",
                catalog_only_snapshot(catalog_entry=absent_catalog()),
                "catalog",
                False,
                "source_missing",
            ),
            (
                "combined one missing",
                both_snapshot(catalog_entry=absent_catalog()),
                "sqlite+catalog",
                False,
                "source_missing",
            ),
            (
                "unreadable",
                db_only_snapshot(db_entry=observed_db(DB_META, readable=False, verified=False)),
                "sqlite",
                True,
                "source_unreadable",
            ),
            (
                "replaced",
                db_only_snapshot(db_entry=observed_db(metadata(HEX_D, 7, 42))),
                "sqlite",
                True,
                "source_replaced",
            ),
            (
                "database invalid",
                db_only_snapshot(db_entry=observed_db(DB_META, verified=False)),
                "sqlite",
                True,
                "database_invalid",
            ),
            (
                "catalog invalid",
                catalog_only_snapshot(catalog_entry=observed_catalog(CAT_META, verified=False)),
                "catalog",
                True,
                "catalog_invalid",
            ),
            (
                "source changed",
                db_only_snapshot(
                    frozen_db=frozen_source(DB_META, HEX_C),
                    db_entry=observed_db(DB_META, content_sha256=HEX_D),
                ),
                "sqlite",
                True,
                "source_changed",
            ),
            (
                "catalog changed",
                catalog_only_snapshot(
                    frozen_catalog=frozen_source(CAT_META, HEX_C),
                    catalog_entry=observed_catalog(CAT_META, content_sha256=HEX_D),
                ),
                "catalog",
                True,
                "catalog_changed",
            ),
            (
                "project collision",
                catalog_only_snapshot(
                    catalog_entry=observed_catalog(CAT_META, project_collision=True)
                ),
                "catalog",
                True,
                "project_collision",
            ),
        )
        for label, snapshot, kind, present, reason in cases:
            with self.subTest(case=label):
                payload = self.assert_contract_payload(
                    snapshot, kind=kind, present=present, ready=False, reason=reason
                )
                self.assertIs(payload["ready"], False)

    def test_identity_uses_observed_metadata_not_the_frozen_record(self):
        frozen_meta = metadata(HEX_A, 7, 42)
        observed_meta = metadata(HEX_D, 8, 43)
        snapshot = db_only_snapshot(
            frozen_db=frozen_source(frozen_meta), db_entry=observed_db(observed_meta)
        )
        payload = d4.build_health_payload(snapshot)
        self.assertEqual(
            payload["dataSource"]["identity"],
            independent_data_source_identity(observed_meta, None),
        )
        self.assertNotEqual(
            payload["dataSource"]["identity"],
            independent_data_source_identity(frozen_meta, None),
        )
        self.assertEqual(payload["reason"], "source_replaced")

    def test_built_payload_is_fresh_and_isolated(self):
        snapshot = both_snapshot()
        first = d4.build_health_payload(snapshot)
        first["dataSource"]["identity"] = HEX_E
        first["dataSource"]["present"] = False
        first["reason"] = "source_missing"
        second = d4.build_health_payload(snapshot)
        self.assertIsNot(first["dataSource"], second["dataSource"])
        self.assertEqual(
            second["dataSource"]["identity"], independent_data_source_identity(DB_META, CAT_META)
        )
        self.assertIs(second["dataSource"]["present"], True)
        self.assertEqual(second["reason"], "ok")
        self.assertIs(second["ready"], True)

    def test_board_digest_and_config_identity_match_independent_formula(self):
        snapshot = both_snapshot()
        payload = d4.build_health_payload(snapshot)
        self.assertEqual(payload["boardSha256"], BOARD_SHA)
        self.assertEqual(
            payload["configIdentity"], independent_config_identity(BOTH_CONFIG, BOARD_SHA)
        )

    def test_snapshot_board_digest_is_carried_and_not_recomputed(self):
        snapshot = db_only_snapshot(board_sha256=HEX_D)
        payload = d4.build_health_payload(snapshot)
        self.assertEqual(payload["boardSha256"], HEX_D)
        self.assertEqual(
            payload["configIdentity"], independent_config_identity(DB_ONLY_CONFIG, HEX_D)
        )

    def test_input_snapshot_is_not_mutated_by_any_public_call(self):
        snapshot = both_snapshot()
        before = json.loads(json.dumps(snapshot))
        expected = {
            "expected_board_sha256": BOARD_SHA,
            "expected_config_identity": independent_config_identity(BOTH_CONFIG, BOARD_SHA),
            "expected_data_source_identity": independent_data_source_identity(DB_META, CAT_META),
        }
        d4.validate_source_snapshot(snapshot)
        d4.classify_source_drift(snapshot)
        d4.build_health_payload(snapshot)
        d4.evaluate_source_snapshot(snapshot, **expected)
        self.assertEqual(snapshot, before)


# --------------------------------------------------------------------------- #
# Privacy and bounded output
# --------------------------------------------------------------------------- #
class PrivacyAndBoundTests(unittest.TestCase):
    def test_serialized_payload_carries_no_paths_tokens_or_free_form_text(self):
        snapshot = both_snapshot()
        payload = d4.build_health_payload(snapshot)
        text = d4.serialize_health_payload(payload).decode("utf-8")
        for forbidden in ("/", "sqlite3", "Traceback", "token", "Error", "exception", "\n", " "):
            self.assertNotIn(forbidden, text)
        for path in (
            snapshot["config"]["dbPath"],
            snapshot["config"]["catalogPath"],
            snapshot["config"]["runtimeDir"],
            snapshot["config"]["boardScript"],
        ):
            self.assertNotIn(path, text)

    def test_payload_leaves_are_bounded_values(self):
        payload = d4.build_health_payload(both_snapshot())
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

    def test_serialize_is_canonical_deterministic_and_bounded(self):
        payload = d4.build_health_payload(both_snapshot())
        first = d4.serialize_health_payload(payload)
        second = d4.serialize_health_payload(dict(payload, dataSource=dict(payload["dataSource"])))
        self.assertEqual(first, second)
        self.assertEqual(first, independent_canonical_bytes(payload))
        self.assertNotIn(b"\n", first)
        self.assertLessEqual(len(first), 1024)
        self.assertEqual(json.loads(first.decode("utf-8")), payload)

    def test_serialize_rejects_unbounded_or_free_form_payloads(self):
        payload = d4.build_health_payload(db_only_snapshot())
        for label, candidate in (
            ("extra top-level field", dict(payload, extra="/opt/d4/data/board.sqlite3")),
            ("free-form reason", dict(payload, reason="boom /opt/d4/data/board.sqlite3")),
            (
                "extra nested field",
                dict(payload, dataSource=dict(payload["dataSource"], path="/opt/d4/data/board.sqlite3")),
            ),
        ):
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    d4.serialize_health_payload(candidate)

    def test_payload_contains_no_service_management_vocabulary(self):
        text = d4.serialize_health_payload(d4.build_health_payload(db_only_snapshot())).decode(
            "utf-8"
        )
        for forbidden in ("healthz", "launchd", "plist", "http", "socket", "restart", "migrat"):
            self.assertNotIn(forbidden, text)


# --------------------------------------------------------------------------- #
# Snapshot-level evaluation against trusted expected digests
# --------------------------------------------------------------------------- #
class EvaluateSourceSnapshotTests(unittest.TestCase):
    def expected_digests(self, snapshot):
        observed_db = snapshot["observed"]["db"]
        observed_catalog = snapshot["observed"]["catalog"]
        return {
            "expected_board_sha256": snapshot["boardSha256"],
            "expected_config_identity": independent_config_identity(
                snapshot["config"], snapshot["boardSha256"]
            ),
            "expected_data_source_identity": independent_data_source_identity(
                observed_db["metadata"] if observed_db is not None else None,
                observed_catalog["metadata"] if observed_catalog is not None else None,
            ),
        }

    def test_matching_ready_snapshot_returns_ok(self):
        for label, snapshot in (
            ("db only", db_only_snapshot()),
            ("catalog only", catalog_only_snapshot()),
            ("both", both_snapshot()),
        ):
            with self.subTest(case=label):
                self.assertEqual(
                    d4.evaluate_source_snapshot(snapshot, **self.expected_digests(snapshot)),
                    (True, "ok"),
                )

    def test_board_mismatch_is_checked_first(self):
        snapshot = db_only_snapshot()
        digests = self.expected_digests(snapshot)
        digests["expected_board_sha256"] = HEX_A
        self.assertEqual(
            d4.evaluate_source_snapshot(snapshot, **digests), (False, "board_mismatch")
        )
        digests["expected_config_identity"] = HEX_A
        self.assertEqual(
            d4.evaluate_source_snapshot(snapshot, **digests), (False, "board_mismatch")
        )

    def test_config_mismatch(self):
        snapshot = db_only_snapshot()
        digests = self.expected_digests(snapshot)
        digests["expected_config_identity"] = HEX_D
        self.assertEqual(
            d4.evaluate_source_snapshot(snapshot, **digests), (False, "config_mismatch")
        )

    def test_data_source_mismatch(self):
        snapshot = db_only_snapshot()
        digests = self.expected_digests(snapshot)
        digests["expected_data_source_identity"] = HEX_D
        self.assertEqual(
            d4.evaluate_source_snapshot(snapshot, **digests), (False, "data_source_mismatch")
        )

    def test_replaced_source_is_a_data_source_mismatch_against_the_frozen_expectation(self):
        frozen_meta = metadata(HEX_A, 7, 42)
        observed_meta = metadata(HEX_D, 7, 42)
        snapshot = db_only_snapshot(
            frozen_db=frozen_source(frozen_meta), db_entry=observed_db(observed_meta)
        )
        digests = {
            "expected_board_sha256": BOARD_SHA,
            "expected_config_identity": independent_config_identity(DB_ONLY_CONFIG, BOARD_SHA),
            "expected_data_source_identity": independent_data_source_identity(frozen_meta, None),
        }
        self.assertEqual(
            d4.evaluate_source_snapshot(snapshot, **digests), (False, "data_source_mismatch")
        )
        # The specific drift code stays available from the classifier.
        self.assertEqual(d4.classify_source_drift(snapshot), "source_replaced")

    def test_identity_match_but_not_ready_returns_the_readiness_reason(self):
        cases = (
            (
                "unreadable",
                db_only_snapshot(db_entry=observed_db(DB_META, readable=False, verified=False)),
                "source_unreadable",
            ),
            (
                "source changed",
                db_only_snapshot(
                    frozen_db=frozen_source(DB_META, HEX_C),
                    db_entry=observed_db(DB_META, content_sha256=HEX_D),
                ),
                "source_changed",
            ),
            (
                "database invalid",
                db_only_snapshot(db_entry=observed_db(DB_META, verified=False)),
                "database_invalid",
            ),
            (
                "project collision",
                catalog_only_snapshot(
                    catalog_entry=observed_catalog(CAT_META, project_collision=True)
                ),
                "project_collision",
            ),
        )
        for label, snapshot, expected_reason in cases:
            with self.subTest(case=label):
                self.assertEqual(
                    d4.evaluate_source_snapshot(snapshot, **self.expected_digests(snapshot)),
                    (False, expected_reason),
                )

    def test_missing_source_mismatches_the_expected_data_source_identity(self):
        # Frozen ordering compares the data-source digest before readiness, and a missing
        # source carries a null identity, so this is reported as a data-source mismatch.
        snapshot = db_only_snapshot(db_entry=absent_db())
        self.assertEqual(
            d4.evaluate_source_snapshot(snapshot, **self.expected_digests(snapshot)),
            (False, "data_source_mismatch"),
        )
        self.assertEqual(d4.classify_source_drift(snapshot), "source_missing")

    def test_invalid_snapshot_returns_invalid_payload(self):
        cases = {
            "not object": [1, 2, 3],
            "extra key": {"bad": True},
            "invalid observation": db_only_snapshot(db_entry=observed_db(DB_META, present=1)),
        }
        for label, snapshot in cases.items():
            with self.subTest(case=label):
                self.assertEqual(
                    d4.evaluate_source_snapshot(
                        snapshot,
                        expected_board_sha256=BOARD_SHA,
                        expected_config_identity=HEX_B,
                        expected_data_source_identity=HEX_C,
                    ),
                    (False, "invalid_payload"),
                )

    def test_bad_expected_digest_raises(self):
        snapshot = db_only_snapshot()
        good = self.expected_digests(snapshot)
        cases = {
            "board": dict(good, expected_board_sha256="X"),
            "config": dict(good, expected_config_identity=False),
            "data": dict(good, expected_data_source_identity="z" * 64),
        }
        for label, digests in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    d4.evaluate_source_snapshot(snapshot, **digests)


# --------------------------------------------------------------------------- #
# Frozen G2-A semantics are preserved
# --------------------------------------------------------------------------- #
class FrozenSemanticsTests(unittest.TestCase):
    def test_config_identity_matches_independent_formula(self):
        for label, config in (
            ("db only", DB_ONLY_CONFIG),
            ("catalog only", CATALOG_ONLY_CONFIG),
            ("both", BOTH_CONFIG),
        ):
            with self.subTest(case=label):
                self.assertEqual(
                    d4.compute_config_identity(config, board_sha256=HEX_D),
                    independent_config_identity(config, HEX_D),
                )

    def test_config_identity_is_order_insensitive_and_leaves_input_unchanged(self):
        config = BOTH_CONFIG
        reordered = {name: config[name] for name in reversed(CONFIG_FIELD_NAMES)}
        before = json.loads(json.dumps(config))
        self.assertEqual(
            d4.compute_config_identity(config, board_sha256=HEX_D),
            d4.compute_config_identity(reordered, board_sha256=HEX_D),
        )
        self.assertEqual(config, before)

    def test_invalid_configs_and_board_digests_raise(self):
        cases = {
            "missing field": dropped(valid_config(), "port"),
            "extra field": dict(valid_config(), extra=1),
            "bad schema": valid_config(schemaVersion="d4-board-app-config.v2"),
            "bad host": valid_config(host="0.0.0.0"),
            "port bool": valid_config(port=True),
            "port low": valid_config(port=80),
            "port high": valid_config(port=70000),
            "port float": valid_config(port=8787.0),
            "db relative": valid_config(dbPath="data/board.sqlite3"),
            "db dotdot": valid_config(dbPath="/opt/../etc/x"),
            "db trailing slash": valid_config(dbPath="/opt/d4/data/"),
            "db repeated slash": valid_config(dbPath="/opt//d4/data"),
            "db tmp": valid_config(dbPath="/tmp/board.sqlite3"),
            "db var folders": valid_config(dbPath="/private/var/folders/x/board.sqlite3"),
            "db empty": valid_config(dbPath=""),
            "db control": valid_config(dbPath="/opt/d4/\x01bad"),
            "both none": valid_config(dbPath=None, catalogPath=None),
            "board basename": valid_config(boardScript="/opt/d4/app/board.py"),
            "python path": valid_config(pythonPath="/usr/bin/python"),
            "label": valid_config(launchAgentLabel="com.example.x"),
            "config not object": [1, 2, 3],
        }
        for label, config in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    d4.compute_config_identity(config, board_sha256=HEX_D)
        for label, sha in {
            "uppercase": "A" * 64,
            "short": "a" * 63,
            "nonhex": "z" * 64,
            "bool": True,
            "int": 1,
        }.items():
            with self.subTest(case="board " + label):
                with self.assertRaises(ValueError):
                    d4.compute_config_identity(valid_config(), board_sha256=sha)

    def test_data_source_identity_matches_independent_formula(self):
        self.assertEqual(
            d4.compute_data_source_identity(db=DB_META),
            independent_data_source_identity(DB_META, None),
        )
        self.assertEqual(
            d4.compute_data_source_identity(catalog=CAT_META),
            independent_data_source_identity(None, CAT_META),
        )
        self.assertEqual(
            d4.compute_data_source_identity(db=DB_META, catalog=CAT_META),
            independent_data_source_identity(DB_META, CAT_META),
        )

    def test_data_source_role_swap_and_invalid_metadata(self):
        other = metadata(HEX_C, 1, 2)
        self.assertNotEqual(
            d4.compute_data_source_identity(db=DB_META, catalog=other),
            d4.compute_data_source_identity(db=other, catalog=DB_META),
        )
        with self.assertRaises(ValueError):
            d4.compute_data_source_identity()
        for label, meta in {
            "string": "not-a-mapping",
            "extra field": dict(DB_META, size=1),
            "bool device": dict(DB_META, device=True),
            "zero inode": dict(DB_META, inode=0),
            "uppercase path sha": dict(DB_META, pathSha256="A" * 64),
        }.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    d4.compute_data_source_identity(db=meta)

    def test_health_payload_validation_returns_isolated_copy(self):
        payload = independent_expected_payload(
            db_only_snapshot(), kind="sqlite", present=True, ready=True, reason="ok"
        )
        validated = d4.validate_health_payload(payload)
        validated["reason"] = "source_missing"
        validated["dataSource"]["kind"] = "catalog"
        payload["dataSource"]["present"] = False
        self.assertEqual(payload["reason"], "ok")
        self.assertEqual(payload["dataSource"]["kind"], "sqlite")
        self.assertIs(validated["dataSource"]["present"], True)

    def test_invalid_health_payloads_raise(self):
        base = independent_expected_payload(
            db_only_snapshot(), kind="sqlite", present=True, ready=True, reason="ok"
        )
        cases = {
            "not object": [1],
            "missing key": dropped(base, "auth"),
            "extra key": dict(base, extra=1),
            "nested extra": dict(base, dataSource=dict(base["dataSource"], extra=1)),
            "dataSource not object": dict(base, dataSource="x"),
            "ready int": dict(base, ready=1),
            "present int": dict(base, dataSource=dict(base["dataSource"], present=1)),
            "kind none": dict(base, dataSource=dict(base["dataSource"], kind="none")),
            "schema": dict(base, schemaVersion="d4-board-health.v2"),
            "service": dict(base, service="other"),
            "auth": dict(base, auth="token"),
            "reason unknown": dict(base, reason="weird"),
            "ready false reason ok": dict(base, ready=False),
            "board sha uppercase": dict(base, boardSha256="A" * 64),
            "identity null when present": dict(
                base, dataSource=dict(base["dataSource"], identity=None)
            ),
            "present false with wrong reason": dict(
                base,
                ready=False,
                reason="source_replaced",
                dataSource=dict(base["dataSource"], present=False, identity=None),
            ),
        }
        for label, payload in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(ValueError):
                    d4.validate_health_payload(payload)

    def test_evaluate_health_payload_order_and_immutability(self):
        payload = independent_expected_payload(
            both_snapshot(), kind="sqlite+catalog", present=True, ready=True, reason="ok"
        )
        good = {
            "expected_board_sha256": payload["boardSha256"],
            "expected_config_identity": payload["configIdentity"],
            "expected_data_source_identity": payload["dataSource"]["identity"],
        }
        before = json.loads(json.dumps(payload))
        self.assertEqual(d4.evaluate_health_payload(payload, **good), (True, "ok"))
        self.assertEqual(
            d4.evaluate_health_payload(payload, **dict(good, expected_board_sha256=HEX_A)),
            (False, "board_mismatch"),
        )
        self.assertEqual(
            d4.evaluate_health_payload(
                payload,
                **dict(good, expected_board_sha256=HEX_A, expected_config_identity=HEX_A)
            ),
            (False, "board_mismatch"),
        )
        self.assertEqual(
            d4.evaluate_health_payload(payload, **dict(good, expected_config_identity=HEX_D)),
            (False, "config_mismatch"),
        )
        self.assertEqual(
            d4.evaluate_health_payload(payload, **dict(good, expected_data_source_identity=HEX_D)),
            (False, "data_source_mismatch"),
        )
        self.assertEqual(d4.evaluate_health_payload({"bad": True}, **good), (False, "invalid_payload"))
        self.assertEqual(payload, before)
        with self.assertRaises(ValueError):
            d4.evaluate_health_payload(payload, **dict(good, expected_board_sha256="X"))

    def test_matched_but_not_ready_returns_payload_reason(self):
        payload = independent_expected_payload(
            db_only_snapshot(), kind="sqlite", present=True, ready=False, reason="source_unreadable"
        )
        self.assertEqual(
            d4.evaluate_health_payload(
                payload,
                expected_board_sha256=payload["boardSha256"],
                expected_config_identity=payload["configIdentity"],
                expected_data_source_identity=payload["dataSource"]["identity"],
            ),
            (False, "source_unreadable"),
        )

    def test_frozen_constants_and_enum_membership_are_unchanged(self):
        self.assertEqual(d4.REASONS, REASONS)
        self.assertEqual(d4.HEALTH_FIELDS, HEALTH_FIELDS)
        self.assertEqual(d4.DATA_SOURCE_FIELDS, DATA_SOURCE_FIELDS)
        self.assertEqual(d4.CONFIG_FIELDS, CONFIG_FIELD_NAMES)
        self.assertEqual(d4.HEALTH_SCHEMA, "d4-board-health.v1")
        self.assertEqual(d4.SERVICE_NAME, "d4-board")
        self.assertEqual(d4.APP_CONFIG_SCHEMA, "d4-board-app-config.v1")
        self.assertEqual(d4.CONFIG_IDENTITY_SCHEMA, "d4-config-identity.v1")
        self.assertEqual(d4.DATA_SOURCE_IDENTITY_SCHEMA, "d4-data-source-identity.v1")
        self.assertEqual(d4.DATASOURCE_KINDS, ("sqlite", "catalog", "sqlite+catalog"))
        self.assertEqual(d4.AUTH_NONE, "none")
        self.assertEqual(d4.ALLOWED_HOST, "127.0.0.1")
        self.assertEqual(d4.LAUNCH_AGENT_LABEL, "com.conductor.d4.board")
        self.assertEqual(d4.BOARD_SCRIPT_BASENAME, "d4_board.py")
        self.assertEqual(d4.ALLOWED_PYTHON_PATH, "/usr/bin/python3")

    def test_returned_digests_are_lowercase_hex64(self):
        values = (
            d4.compute_config_identity(BOTH_CONFIG, board_sha256=BOARD_SHA),
            d4.compute_data_source_identity(db=DB_META, catalog=CAT_META),
        )
        for value in values:
            self.assertEqual(len(value), 64)
            self.assertEqual(value, value.lower())
            self.assertTrue(all(char in LOWER_HEX for char in value))


# --------------------------------------------------------------------------- #
# Module surface: offline, no I/O capabilities, no CLI, no service wiring
# --------------------------------------------------------------------------- #
class ModuleSurfaceTests(unittest.TestCase):
    def test_public_api_surface(self):
        self.assertEqual(
            set(d4.__all__),
            {
                "compute_config_identity",
                "compute_data_source_identity",
                "validate_health_payload",
                "evaluate_health_payload",
                "validate_source_snapshot",
                "classify_source_drift",
                "build_health_payload",
                "serialize_health_payload",
                "evaluate_source_snapshot",
            },
        )
        for name in d4.__all__:
            self.assertTrue(callable(getattr(d4, name)))

    def test_module_binds_no_io_capable_modules(self):
        for name in (
            "os",
            "io",
            "sys",
            "sqlite3",
            "socket",
            "subprocess",
            "http",
            "urllib",
            "shutil",
            "pathlib",
            "plistlib",
            "tempfile",
            "threading",
            "asyncio",
            "select",
            "ssl",
            "requests",
            "glob",
            "inspect",
            "time",
        ):
            self.assertNotIn(name, vars(d4))

    def test_module_has_no_cli_or_service_entrypoint(self):
        self.assertFalse(hasattr(d4, "main"))
        for name in vars(d4):
            lowered = name.lower()
            for forbidden in ("server", "handler", "launchd", "healthz", "plist", "migrat", "restart"):
                self.assertNotIn(forbidden, lowered)

    def test_module_reports_no_rollout_or_readiness_claim(self):
        self.assertFalse(hasattr(d4, "is_ready"))
        self.assertFalse(hasattr(d4, "probe"))
        self.assertFalse(hasattr(d4, "install"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
