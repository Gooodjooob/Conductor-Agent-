"""Standalone unittest suite for the G2-B configured ``/healthz`` endpoint wiring.

NOT_RUN in this attempt
-----------------------
This worker has ``Bash=[]``: no command was executed, no test was run and no SHA was
recomputed in this round. Every case below is therefore NOT_RUN, not PASS; the actual run
and the independent scope/SHA verification belong to cox.

Scope
-----
* ``candidate/scripts/d4_board.py``: the keyword-only ``health_config`` /
  ``expected_board_sha256`` handler wiring (freeze-once before the handler class exists,
  exact db/catalog source match, configured ``GET /healthz``, legacy unconfigured 503,
  unchanged static / read-only API / 405 write rejection) and the optional
  ``--runtime-dir`` fixed-configuration CLI path.
* ``candidate/scripts/d4_app_config.py``: the ``--runtime-dir`` slot in the fixed argv
  (after the sources, before ``--port``, ``--port`` still last) and the fact that the
  interpreter-delivered form of that argv is accepted by the board CLI.

Fixtures
--------
Every fixture lives in one ``tempfile.mkdtemp`` directory created under
``candidate/tests/test-artifacts`` for a single test and removed again in cleanup. The
frozen protocol rejects configured paths under ``/tmp``, ``/private/tmp``, ``/var/folders``
and ``/private/var/folders``, so no fixture is ever placed there. The frozen manifest input
``candidate/tests/fixtures/d4_readiness_schema.sql`` is executed read-only and is never
modified or copied. Positive database fixtures use that complete schema, so the collector
sees the real D4 storage contract.

Only ephemeral loopback ``ThreadingHTTPServer`` instances built from test-owned fixtures
are used, and every one of them is shut down, closed and joined. There is no real user
listener, no installed or loaded launch agent, no service change, no migration and no
business write. The frozen endpoint tests (``test_d4_board.py``,
``test_d4_board_summary_contract.py``) are neither imported nor executed here.

The candidate modules are loaded from their exact relative file paths through importlib
file specs: no directory scan, no environment override and no import of the frozen input
modules.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request, urlopen

_TESTS_DIR = Path(__file__).resolve().parent
_SCRIPTS_DIR = _TESTS_DIR.parent / "scripts"
_TEST_ARTIFACTS = _TESTS_DIR / "test-artifacts"
_SCHEMA_FIXTURE = _TESTS_DIR / "fixtures" / "d4_readiness_schema.sql"


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _SCRIPTS_DIR / filename)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load candidate module")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


board = _load("d4_board_endpoint_under_test", "d4_board.py")
appcfg = _load("d4_app_config_endpoint_under_test", "d4_app_config.py")

BOARD_SOURCE = '"""D4 board endpoint test double (fixture only)."""\nVALUE = 1\n'
PYTHON_PATH = "/usr/bin/python3"
PYTHON_AVAILABLE = os.path.isfile(PYTHON_PATH) and os.access(PYTHON_PATH, os.X_OK)
requires_python = unittest.skipUnless(
    PYTHON_AVAILABLE, "%s is not an executable file on this host" % PYTHON_PATH
)

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

HEALTH_HEADERS = {
    "Content-Type": "application/json;charset=utf8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def canonical_bytes(obj):
    """The frozen canonical-bytes rule, re-implemented independently of the candidate."""
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def digest_of(obj):
    return hashlib.sha256(canonical_bytes(obj)).hexdigest()


def path_digest(path):
    return hashlib.sha256(str(path).encode("utf-8")).hexdigest()


def observed_metadata(path):
    info = os.lstat(str(path))
    return {
        "pathSha256": path_digest(path),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


def config_identity(config, board_sha):
    """Independent normative config-instance identity (does not call the implementation)."""
    return digest_of(
        {
            "schemaVersion": "d4-config-identity.v1",
            "boardSha256": board_sha,
            "config": {name: config[name] for name in CONFIG_FIELD_NAMES},
        }
    )


def data_source_identity(db_metadata, catalog_metadata):
    """Independent normative data-source identity (does not call the implementation)."""

    def clean(metadata):
        if metadata is None:
            return None
        return {
            "pathSha256": metadata["pathSha256"],
            "device": metadata["device"],
            "inode": metadata["inode"],
        }

    return digest_of(
        {
            "schemaVersion": "d4-data-source-identity.v1",
            "db": clean(db_metadata),
            "catalog": clean(catalog_metadata),
        }
    )


def catalog_document(*, project_id="d4-product", name="D4 看板", task_id="task-catalog-1",
                     status="verified"):
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
                        "documents": [],
                    }
                ],
                "issues": [],
            }
        ],
    }


class _JumpToBind(Exception):
    """Test-local sentinel: raised by the stubbed server constructor instead of binding."""


class EndpointCase(unittest.TestCase):
    """One self-cleaning fixture root under ``candidate/tests/test-artifacts``."""

    def setUp(self):
        _TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.root = Path(
            tempfile.mkdtemp(prefix="d4-health-endpoint-", dir=str(_TEST_ARTIFACTS))
        )
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.app_dir = self.root / "app"
        self.app_dir.mkdir()
        self.board_script = self.app_dir / "d4_board.py"
        self.board_script.write_text(BOARD_SOURCE, encoding="utf-8")
        self.board_sha = hashlib.sha256(BOARD_SOURCE.encode("utf-8")).hexdigest()
        self.db_path = self.root / "board.sqlite3"
        self.catalog_path = self.root / "catalog.json"
        self.runtime_dir = self.root / "runtime"
        (self.runtime_dir / "logs").mkdir(parents=True)
        self.write_database()
        self.write_catalog()

    # -- fixture writers (test-owned temporary directory only) ------------------
    def write_database(self, path=None, *, projects=("live-project",),
                       tasks=(("task-live-1", "live-project"),), schema=True):
        target = Path(path) if path is not None else self.db_path
        if target.exists() or target.is_symlink():
            target.unlink()
        connection = sqlite3.connect(str(target))
        try:
            if schema:
                connection.executescript(_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
            else:
                connection.execute("CREATE TABLE placeholder (value TEXT)")
            for project_id in projects:
                connection.execute(
                    "INSERT INTO workspace_projects "
                    "(project_id, name, goal, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (project_id, project_id, project_id,
                     "2026-01-01T00:00:00Z", "2026-01-01T00:00:00Z"),
                )
            for task_id, project_id in tasks:
                connection.execute(
                    "INSERT INTO tasks (task_id, project_id) VALUES (?, ?)",
                    (task_id, project_id),
                )
            connection.commit()
        finally:
            connection.close()
        return target

    def write_catalog(self, path=None, *, project_id="d4-product", name="D4 看板"):
        target = Path(path) if path is not None else self.catalog_path
        document = catalog_document(project_id=project_id, name=name)
        target.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )
        return target

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

    def allowed_root(self, config):
        return os.path.commonpath(
            [os.path.abspath(config[name]) for name in
             ("boardScript", "runtimeDir", "dbPath", "catalogPath")
             if config[name] is not None]
        )

    def configured_handler(self, **overrides):
        return board.make_handler(
            self.db_path,
            catalog_path=self.catalog_path,
            health_config=self.config(**overrides),
            expected_board_sha256=self.board_sha,
        )

    # -- ephemeral loopback server (always shut down, closed and joined) --------
    def serve(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self._stop_server, server, thread)
        return "http://127.0.0.1:%d" % server.server_port

    @staticmethod
    def _stop_server(server, thread):
        server.shutdown()
        server.server_close()
        thread.join()

    def request(self, base, path, method="GET", data=None):
        request = Request(base + path, data=data, method=method)
        try:
            response = urlopen(request, timeout=10)
        except HTTPError as error:
            try:
                return error.code, error.headers, error.read()
            finally:
                error.close()
        with response:
            return response.status, response.headers, response.read()

    def payload_of(self, status, body):
        self.assertIn(status, (200, 503))
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(tuple(sorted(payload)), tuple(sorted(HEALTH_FIELDS)))
        self.assertEqual(tuple(sorted(payload["dataSource"])),
                         tuple(sorted(DATA_SOURCE_FIELDS)))
        self.assertEqual(body, canonical_bytes(payload))
        return payload


class ConfiguredHealthTests(EndpointCase):
    def test_configured_health_returns_the_exact_ready_payload(self):
        base = self.serve(self.configured_handler())
        status, headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 200)
        payload = self.payload_of(status, body)
        self.assertEqual(payload["schemaVersion"], "d4-board-health.v1")
        self.assertEqual(payload["service"], "d4-board")
        self.assertEqual(payload["boardSha256"], self.board_sha)
        self.assertEqual(
            payload["configIdentity"], config_identity(self.config(), self.board_sha)
        )
        self.assertEqual(payload["dataSource"]["kind"], "sqlite+catalog")
        self.assertIs(payload["dataSource"]["present"], True)
        self.assertEqual(
            payload["dataSource"]["identity"],
            data_source_identity(
                observed_metadata(self.db_path), observed_metadata(self.catalog_path)
            ),
        )
        self.assertIs(payload["ready"], True)
        self.assertEqual(payload["reason"], "ok")
        self.assertEqual(payload["auth"], "none")
        for name, value in HEALTH_HEADERS.items():
            self.assertEqual(headers[name], value, "header %s" % name)

    def test_repeated_requests_are_byte_identical(self):
        base = self.serve(self.configured_handler())
        first_status, _first_headers, first_body = self.request(base, "/healthz")
        second_status, _second_headers, second_body = self.request(base, "/healthz")
        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 200)
        self.assertEqual(first_body, second_body)

    def test_missing_source_is_503_unready_and_leaks_nothing(self):
        base = self.serve(self.configured_handler())
        self.assertEqual(self.request(base, "/healthz")[0], 200)
        os.remove(str(self.db_path))
        status, headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 503)
        payload = self.payload_of(status, body)
        self.assertIs(payload["ready"], False)
        self.assertEqual(payload["reason"], "source_missing")
        self.assertEqual(payload["dataSource"]["kind"], "sqlite+catalog")
        self.assertIs(payload["dataSource"]["present"], False)
        self.assertIsNone(payload["dataSource"]["identity"])
        self.assertEqual(headers["Content-Type"], "application/json;charset=utf8")

    def test_replaced_source_is_seen_without_refreeze(self):
        replacement = self.root / "replacement.sqlite3"
        self.write_database(replacement)
        base = self.serve(self.configured_handler())
        first_status, _headers, first_body = self.request(base, "/healthz")
        self.assertEqual(first_status, 200)
        first = json.loads(first_body.decode("utf-8"))
        os.replace(str(replacement), str(self.db_path))
        status, _headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 503)
        payload = self.payload_of(status, body)
        self.assertIs(payload["ready"], False)
        self.assertEqual(payload["reason"], "source_replaced")
        # Same frozen identity (board digests unchanged) but a new observed data source.
        self.assertEqual(payload["boardSha256"], first["boardSha256"])
        self.assertEqual(payload["configIdentity"], first["configIdentity"])
        self.assertNotEqual(payload["dataSource"]["identity"], first["dataSource"]["identity"])
        self.assertEqual(
            payload["dataSource"]["identity"],
            data_source_identity(
                observed_metadata(self.db_path), observed_metadata(self.catalog_path)
            ),
        )

    def test_changed_catalog_content_is_seen_without_refreeze(self):
        base = self.serve(self.configured_handler())
        self.assertEqual(self.request(base, "/healthz")[0], 200)
        self.write_catalog(name="D4 看板（已修改）")
        status, _headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 503)
        payload = self.payload_of(status, body)
        self.assertIs(payload["ready"], False)
        self.assertEqual(payload["reason"], "catalog_changed")

    def test_health_payload_carries_no_path_token_or_slash(self):
        base = self.serve(self.configured_handler())
        status, _headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 200)
        text = body.decode("utf-8")
        for secret in (str(self.root), str(self.db_path), str(self.catalog_path),
                       str(self.board_script), str(self.runtime_dir)):
            self.assertNotIn(secret, text, "payload must not leak %s" % secret)
        self.assertNotIn("/", text, "the bounded payload must contain no path separator")

    def test_unexpected_collector_failure_is_a_generic_503(self):
        base = self.serve(self.configured_handler())
        with mock.patch.object(
            board._HEALTH, "collect_health", side_effect=RuntimeError("synthetic collector failure")
        ):
            status, headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "health_read_failed"})
        self.assertEqual(headers["Content-Type"], "application/json;charset=utf8")
        self.assertNotIn("synthetic", body.decode("utf-8"))


class LegacySurfaceTests(EndpointCase):
    def test_legacy_handler_serves_an_unconfigured_503_health(self):
        base = self.serve(board.make_handler(self.db_path))
        status, headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "health_unconfigured"})
        self.assertEqual(headers["Content-Type"], "application/json;charset=utf8")
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_static_api_and_read_only_405_are_unchanged(self):
        base = self.serve(board.make_handler(None, catalog_path=self.catalog_path))
        status, headers, body = self.request(base, "/")
        self.assertEqual(status, 200)
        self.assertTrue(headers["Content-Type"].startswith("text/html"))
        self.assertEqual(body, (board.ASSETS / "index.html").read_bytes())
        status, _headers, body = self.request(base, "/api/projects")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body.decode("utf-8"))["projects"][0]["projectId"], "d4-product"
        )
        status, _headers, _body = self.request(base, "/api/not-a-route")
        self.assertEqual(status, 404)
        status, _headers, body = self.request(base, "/healthz", method="POST", data=b"{}")
        self.assertEqual(status, 405)
        self.assertEqual(json.loads(body.decode("utf-8")), {"error": "read_only"})


class HandlerConfigurationTests(EndpointCase):
    def test_health_config_and_expected_sha_must_be_given_together(self):
        with mock.patch.object(board, "ThreadingHTTPServer",
                               side_effect=AssertionError("must not bind")):
            with self.assertRaises(ValueError):
                board.make_handler(self.db_path, catalog_path=self.catalog_path,
                                   health_config=self.config())
            with self.assertRaises(ValueError):
                board.make_handler(self.db_path, catalog_path=self.catalog_path,
                                   expected_board_sha256=self.board_sha)

    def test_mismatched_health_sources_fail_before_binding(self):
        other = self.root / "other.sqlite3"
        self.write_database(other)
        cases = {
            "db mismatch": self.config(dbPath=str(other)),
            "catalog mismatch": self.config(catalogPath=None),
            "missing dbPath field": {k: v for k, v in self.config().items() if k != "dbPath"},
            "not a config": [1, 2, 3],
        }
        for label, config in cases.items():
            with self.subTest(case=label):
                with mock.patch.object(board, "ThreadingHTTPServer",
                                       side_effect=AssertionError("must not bind")):
                    with self.assertRaises(ValueError):
                        board.make_handler(self.db_path, catalog_path=self.catalog_path,
                                           health_config=config,
                                           expected_board_sha256=self.board_sha)

    def test_unfreezable_configuration_fails_before_binding(self):
        real_error = board._HEALTH.HealthRuntimeError
        with mock.patch.object(board, "ThreadingHTTPServer",
                               side_effect=AssertionError("must not bind")):
            with self.assertRaises(real_error) as caught:
                board.make_handler(self.db_path, catalog_path=self.catalog_path,
                                   health_config=self.config(),
                                   expected_board_sha256="0" * 64)
        self.assertEqual(caught.exception.code, "board_sha_unavailable")
        self.assertNotIn("/", str(caught.exception))

    @requires_python
    def test_g1_app_config_object_is_accepted_and_serves_ready_health(self):
        config = self.config()
        app_config = appcfg.validate_config(
            config,
            allowed_root=self.allowed_root(config),
            expected_board_sha256=self.board_sha,
        )
        self.assertIsInstance(app_config, appcfg.AppConfig)
        base = self.serve(
            board.make_handler(self.db_path, catalog_path=self.catalog_path,
                               health_config=app_config,
                               expected_board_sha256=self.board_sha)
        )
        status, _headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 200)
        payload = self.payload_of(status, body)
        self.assertEqual(payload["configIdentity"], config_identity(config, self.board_sha))
        self.assertIs(payload["ready"], True)

    @requires_python
    def test_app_argv_keeps_runtime_dir_before_port(self):
        config = self.config()
        app_config = appcfg.validate_config(
            config,
            allowed_root=self.allowed_root(config),
            expected_board_sha256=self.board_sha,
        )
        argv = appcfg.build_board_argv(app_config)
        self.assertEqual(argv[:2], (PYTHON_PATH, "-B"))
        self.assertEqual(argv[2], config["boardScript"])
        self.assertEqual(argv[-4], "--runtime-dir")
        self.assertEqual(argv[-3], config["runtimeDir"])
        self.assertEqual(argv[-2], "--port")
        self.assertEqual(argv[-1], str(config["port"]))
        self.assertEqual(argv.count("--runtime-dir"), 1)
        self.assertEqual(argv.count("--port"), 1)


class CliRuntimeTests(EndpointCase):
    def runtime_argv(self, **overrides):
        values = {
            "--db": str(self.db_path),
            "--catalog": str(self.catalog_path),
            "--runtime-dir": str(self.runtime_dir),
            "--port": "8787",
        }
        values.update(overrides)
        argv = []
        for name, value in values.items():
            argv.extend([name, value])
        return argv

    def assert_no_bind(self):
        return mock.patch.object(board, "ThreadingHTTPServer",
                                 side_effect=AssertionError("must not bind"))

    def test_invalid_fixed_port_fails_before_bind(self):
        # The frozen G1 validator checks the port before the interpreter identity, so this
        # case needs no interpreter patch and must fail for the port rule itself.
        for port in (0, 80, 1023, 65536, 70000):
            with self.subTest(port=port):
                with self.assert_no_bind():
                    with self.assertRaises(ValueError) as caught:
                        board.main(self.runtime_argv(**{"--port": str(port)}))
                self.assertIn("port", str(caught.exception))
        self.assertTrue(self.runtime_dir.is_dir())

    @requires_python
    def test_invalid_runtime_paths_fail_before_bind_without_creating_anything(self):
        missing_runtime = self.root / "missing-runtime"
        bare_runtime = self.root / "bare-runtime"
        bare_runtime.mkdir()
        before = sorted(os.listdir(str(self.root)))
        cases = (
            ("missing runtime dir", {"--runtime-dir": str(missing_runtime)}, "runtimeDir"),
            ("runtime without logs dir", {"--runtime-dir": str(bare_runtime)}, "logs"),
        )
        for label, overrides, needle in cases:
            with self.subTest(case=label):
                with self.assert_no_bind():
                    with self.assertRaises(ValueError) as caught:
                        board.main(self.runtime_argv(**overrides))
                self.assertIn(needle, str(caught.exception))
        self.assertEqual(sorted(os.listdir(str(self.root))), before)
        self.assertFalse(missing_runtime.exists())
        self.assertNotIn("logs", os.listdir(str(bare_runtime)))

    def test_missing_db_file_fails_before_bind(self):
        missing_db = self.root / "missing.sqlite3"
        with self.assert_no_bind():
            with self.assertRaises(SystemExit):
                board.main(self.runtime_argv(**{"--db": str(missing_db)}))
        self.assertFalse(missing_db.exists())

    def test_temporary_runtime_dir_fails_before_bind(self):
        # A `/tmp` runtime dir makes the derived common root temporary as well, so the
        # frozen G1 filesystem boundary refuses the configuration before anything is bound.
        with self.assert_no_bind():
            with self.assertRaises(ValueError):
                board.main(self.runtime_argv(**{"--runtime-dir": "/tmp/d4-runtime"}))
        self.assertFalse(Path("/tmp/d4-runtime").exists())

    def test_free_interpreter_override_is_rejected_before_bind(self):
        with self.assert_no_bind():
            with self.assertRaises(SystemExit):
                board.main(self.runtime_argv() + ["--python-path", "/usr/bin/python"])

    @requires_python
    def test_app_argv_runtime_mode_serves_the_configured_health(self):
        """The App argv's interpreter-delivered form drives a configured ``/healthz``."""
        config = self.config()
        app_config = appcfg.validate_config(
            config,
            allowed_root=self.allowed_root(config),
            expected_board_sha256=self.board_sha,
        )
        argv = list(appcfg.build_board_argv(app_config))
        self.assertEqual(argv[:2], [PYTHON_PATH, "-B"])
        self.assertTrue(argv[2].endswith("d4_board.py"))
        captured = {}

        def capture_server(address, handler):
            captured["address"] = address
            captured["handler"] = handler
            raise _JumpToBind(address)

        with mock.patch.object(board, "ThreadingHTTPServer", capture_server):
            with self.assertRaises(_JumpToBind):
                board.main(argv[3:])
        self.assertEqual(captured["address"], ("127.0.0.1", 8787))
        base = self.serve(captured["handler"])
        status, _headers, body = self.request(base, "/healthz")
        self.assertEqual(status, 200)
        payload = self.payload_of(status, body)
        self.assertEqual(
            payload["boardSha256"],
            hashlib.sha256(Path(board.__file__).resolve().read_bytes()).hexdigest(),
        )
        self.assertIs(payload["ready"], True)
        self.assertEqual(payload["reason"], "ok")
        self.assertEqual(os.listdir(str(self.runtime_dir)), ["logs"])


if __name__ == "__main__":
    unittest.main()
