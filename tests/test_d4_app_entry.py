"""Independent unittest suite for candidate/scripts/d4_app_entry.py (G2-C orchestrator).

NOT_RUN in this attempt
-----------------------
This worker has ``Bash=[]``: no command was executed, no test was run and no SHA was
recomputed in this round. Every case below is therefore NOT_RUN, not PASS; the actual run
and the independent scope/SHA verification belong to cox.

Scope
-----
* ``launch`` ordering and fail-closed behaviour: G1 config validation first, then the
  frozen expected identity and expected source readiness, then the bounded probe, and
  finally the single ``open`` only on a proven matching ready response.
* The default loopback transport: fixed `127.0.0.1` : fixed config port : `/healthz`,
  1 s timeout, bounded ``MAX_RESPONSE_BYTES + 1`` read, explicit status/header validation,
  strict UTF-8 and strict JSON (duplicate keys / non-finite numbers rejected), and a
  *proven* ``ConnectionRefusedError`` surfaced as the only start trigger.
* The refusal path: exactly one fixed-label ``kickstart`` (never ``-k``/bootstrap/install),
  ``setup_required`` when unregistered, finite 0.2 s polls, a second refusal never kicking
  again, malformed responses stopping immediately and a generic ``probe_exhausted``.
* The default process adapters: exact ``/bin/launchctl`` / ``/usr/bin/open`` argv, bounded
  timeout and no shell (``subprocess.run`` is mocked; nothing is executed).
* The CLI: explicit arguments, regular no-follow bounded config read, pinned digest
  verified before ``launch``, and exit 0 only on a successful open.

Fixtures / safety
-----------------
Every fixture lives in one ``tempfile.mkdtemp`` directory created under
``candidate/tests/test-artifacts`` for a single test and removed again in cleanup. The
frozen G1 validator rejects configured paths under ``/tmp``, ``/private/tmp``,
``/var/folders`` and ``/private/var/folders``, so no fixture is ever placed there. The
frozen manifest input ``candidate/tests/fixtures/d4_readiness_schema.sql`` is executed
read-only and is never modified or copied.

All default service/open/transport actions are mocked: no real socket is opened, no user
listener, no ``launchctl`` and no ``open`` is ever executed. The default transport is
exercised only through a mocked opener that returns crafted bytes.

The candidate modules are loaded from their exact relative file paths through importlib
file specs (``sys.modules`` is registered before ``exec_module``): no directory scan, no
environment override and no import of the frozen input modules.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

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


entry = _load("d4_app_entry", "d4_app_entry.py")
rt = _load("d4_health_runtime_for_entry_tests", "d4_health_runtime.py")

PYTHON_PATH = "/usr/bin/python3"
PYTHON_AVAILABLE = os.path.isfile(PYTHON_PATH) and os.access(PYTHON_PATH, os.X_OK)
requires_python = unittest.skipUnless(
    PYTHON_AVAILABLE, "%s is not an executable file on this host" % PYTHON_PATH
)

BOARD_SOURCE = '"""D4 board entry test double (fixture only)."""\nprint("fixture")\n'

HEALTH_HEADERS = {
    "Content-Type": "application/json;charset=utf8",
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}


def canonical_bytes(obj):
    return json.dumps(
        obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def health_headers(**overrides):
    headers = dict(HEALTH_HEADERS)
    headers.update(overrides)
    return headers


def catalog_document(*, project_id="d4-product", task_id="task-catalog-1", status="verified"):
    return {
        "schemaVersion": "project-catalog.v1",
        "projects": [
            {
                "projectId": project_id,
                "name": "D4 看板",
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


class _FakeResponse:
    def __init__(self, status=200, headers=None, body=b"{}"):
        self.status = status
        self.headers = dict(HEALTH_HEADERS) if headers is None else headers
        self._body = body
        self.reads = []
        self.closed = False

    def read(self, amount=None):
        self.reads.append(amount)
        return self._body if amount is None else self._body[:amount]

    def close(self):
        self.closed = True


class _FakeOpener:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def open(self, request, timeout=None):
        self.calls.append((request, timeout))
        if self.error is not None:
            raise self.error
        return self.response


class EntryFixtureTestCase(unittest.TestCase):
    def setUp(self):
        _TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self.root = Path(
            tempfile.mkdtemp(prefix="d4-app-entry-fixtures-", dir=str(_TEST_ARTIFACTS))
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
        self.runtime_dir.mkdir()
        (self.runtime_dir / "logs").mkdir()

    # -- fixture writers -----------------------------------------------------
    def write_database(self, *, projects=(), schema=True):
        if self.db_path.exists() or self.db_path.is_symlink():
            self.db_path.unlink()
        connection = sqlite3.connect(str(self.db_path))
        try:
            if schema:
                connection.executescript(_SCHEMA_FIXTURE.read_text(encoding="utf-8"))
            else:
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
            connection.commit()
        finally:
            connection.close()

    def write_catalog(self, document=None):
        payload = catalog_document() if document is None else document
        self.catalog_path.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

    def config(self, **overrides):
        config = {
            "schemaVersion": "d4-board-app-config.v1",
            "host": "127.0.0.1",
            "port": 8787,
            "dbPath": str(self.db_path),
            "catalogPath": str(self.catalog_path),
            "pythonPath": "/usr/bin/python3",
            "boardScript": str(self.board_script),
            "runtimeDir": str(self.runtime_dir),
            "launchAgentLabel": "com.conductor.d4.board",
        }
        config.update(overrides)
        return config

    def prepare_ready(self):
        """Valid, non-colliding fixture: live project ids differ from the catalog id."""
        self.write_database(projects=("d4-product-live",))
        self.write_catalog()
        return self.config()

    def build_ready_payload(self):
        """A genuinely ready payload produced by the frozen runtime for this fixture."""
        config = self.prepare_ready()
        frozen = rt.freeze_identity(config, expected_board_sha256=self.board_sha)
        return config, rt.collect_health(frozen)

    def launch(self, config, *, fetch, installed=None, kick=None, openb=None, sleep=None):
        return entry.launch(
            config,
            allowed_root=str(self.root),
            expected_board_sha256=self.board_sha,
            fetch_health=fetch,
            service_installed=installed if installed is not None else mock.Mock(return_value=True),
            kickstart=kick if kick is not None else mock.Mock(return_value=True),
            open_browser=openb if openb is not None else mock.Mock(return_value=True),
            sleep=sleep if sleep is not None else mock.Mock(),
        )

    def raw_launch(self, config, *, fetch, installed=None, kick=None, openb=None, sleep=None):
        """Launch with the *default* transport (``fetch_health=None``)."""
        return entry.launch(
            config,
            allowed_root=str(self.root),
            expected_board_sha256=self.board_sha,
            fetch_health=None,
            service_installed=installed if installed is not None else mock.Mock(return_value=True),
            kickstart=kick if kick is not None else mock.Mock(return_value=True),
            open_browser=openb if openb is not None else mock.Mock(return_value=True),
            sleep=sleep if sleep is not None else mock.Mock(),
        )


# --------------------------------------------------------------------------- #
# Fixed literals
# --------------------------------------------------------------------------- #
class LiteralTests(unittest.TestCase):
    def test_fixed_literals(self):
        self.assertEqual(entry.HEALTH_HOST, "127.0.0.1")
        self.assertEqual(entry.HEALTH_PATH, "/healthz")
        self.assertEqual(entry.HEALTH_TIMEOUT, 1.0)
        self.assertEqual(entry.MAX_RESPONSE_BYTES, 16 * 1024)
        self.assertEqual(entry.MAX_CONFIG_BYTES, 16 * 1024)
        self.assertEqual(entry.PROBE_ATTEMPTS, 10)
        self.assertEqual(entry.PROBE_SLEEP_SECONDS, 0.2)
        self.assertEqual(entry.LAUNCHCTL_PATH, "/bin/launchctl")
        self.assertEqual(entry.OPEN_PATH, "/usr/bin/open")
        self.assertEqual(entry.LAUNCH_AGENT_LABEL, "com.conductor.d4.board")

    def test_result_vocabulary_is_bounded(self):
        self.assertEqual(
            entry._result("failed", "invalid_request"),
            {"status": "failed", "reason": "invalid_request"},
        )
        with self.assertRaises(ValueError):
            entry._result("nope", "invalid_request")
        with self.assertRaises(ValueError):
            entry._result("failed", "definitely-not-a-reason")


# --------------------------------------------------------------------------- #
# Strict response parsing (pure)
# --------------------------------------------------------------------------- #
class ParseResponseTests(unittest.TestCase):
    def _body(self, payload):
        return canonical_bytes(payload)

    def test_accepts_exact_endpoint_content_type_spelling(self):
        payload = {"ready": False, "reason": "source_missing"}
        status, parsed = entry._parse_health_response(503, health_headers(), self._body(payload))
        self.assertEqual(status, 503)
        self.assertEqual(parsed, payload)
        # Canonical spelling and case-insensitive no-store/nosniff also pass.
        entry._parse_health_response(
            503,
            {
                "Content-Type": "application/json; charset=UTF-8",
                "Cache-Control": "private, No-Store",
                "X-Content-Type-Options": "NOSNIFF",
            },
            b"{}",
        )

    def test_rejects_status_outside_allowlist(self):
        for status in (301, 302, 400, 500, 100):
            with self.subTest(status=status):
                with self.assertRaises(entry._HealthFailure) as caught:
                    entry._parse_health_response(status, health_headers(), b"{}")
                self.assertEqual(caught.exception.code, "status_invalid")

    def test_rejects_header_mismatch(self):
        cases = {
            "wrong type": health_headers(**{"Content-Type": "text/html"}),
            "no charset": health_headers(**{"Content-Type": "application/json"}),
            "wrong charset": health_headers(**{"Content-Type": "application/json;charset=latin-1"}),
            "missing no-store": {
                "Content-Type": "application/json;charset=utf8",
                "X-Content-Type-Options": "nosniff",
            },
            "missing nosniff": {
                "Content-Type": "application/json;charset=utf8",
                "Cache-Control": "no-store",
            },
        }
        for label, headers in cases.items():
            with self.subTest(case=label):
                with self.assertRaises(entry._HealthFailure) as caught:
                    entry._parse_health_response(200, headers, b"{}")
                self.assertEqual(caught.exception.code, "header_invalid")

    def test_rejects_oversize_body(self):
        with self.assertRaises(entry._HealthFailure) as caught:
            entry._parse_health_response(
                200, health_headers(), b"x" * (entry.MAX_RESPONSE_BYTES + 1)
            )
        self.assertEqual(caught.exception.code, "oversize")

    def test_exactly_max_body_is_not_oversize(self):
        # Exactly MAX_RESPONSE_BYTES is within bound; it only fails as invalid JSON.
        with self.assertRaises(entry._HealthFailure) as caught:
            entry._parse_health_response(200, health_headers(), b"x" * entry.MAX_RESPONSE_BYTES)
        self.assertEqual(caught.exception.code, "malformed_response")

    def test_rejects_non_utf8_body(self):
        with self.assertRaises(entry._HealthFailure) as caught:
            entry._parse_health_response(200, health_headers(), b'{"a":"\xff"}')
        self.assertEqual(caught.exception.code, "malformed_response")

    def test_rejects_duplicate_keys_and_non_finite_numbers(self):
        for body in (b'{"ready":true,"ready":false}', b'{"v":NaN}', b'{"v":Infinity}', b'{"v":-Infinity}'):
            with self.subTest(body=body):
                with self.assertRaises(entry._HealthFailure) as caught:
                    entry._parse_health_response(200, health_headers(), body)
                self.assertEqual(caught.exception.code, "malformed_response")

    def test_redirect_handler_never_follows(self):
        handler = entry._NoRedirectHandler()
        self.assertIsNone(
            handler.redirect_request(None, None, 302, "Found", {}, "http://example.invalid/")
        )

    def test_opener_disables_redirects_and_uses_empty_proxies(self):
        opener = entry._build_opener()
        self.assertTrue(any(isinstance(handler, entry._NoRedirectHandler) for handler in opener.handlers))
        self.assertFalse(
            any(type(handler) is urllib.request.HTTPRedirectHandler for handler in opener.handlers)
        )
        proxies = [handler for handler in opener.handlers if type(handler) is urllib.request.ProxyHandler]
        # urllib omits an empty ProxyHandler from handlers because it has no methods.
        self.assertEqual(proxies, [])
        self.assertFalse(any(hasattr(handler, "proxy_open") for handler in opener.handlers))


# --------------------------------------------------------------------------- #
# Default loopback transport (opener mocked; no socket)
# --------------------------------------------------------------------------- #
class DefaultTransportTests(unittest.TestCase):
    def test_open_health_uses_fixed_url_get_timeout_and_bounded_read(self):
        response = _FakeResponse(body=b'{"ready":false}')
        opener = _FakeOpener(response=response)
        with mock.patch.object(entry, "_build_opener", return_value=opener):
            status, headers, body = entry._open_health(8787)
        (request, timeout) = opener.calls[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:8787/healthz")
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(timeout, entry.HEALTH_TIMEOUT)
        self.assertEqual(response.reads, [entry.MAX_RESPONSE_BYTES + 1])
        self.assertTrue(response.closed)
        self.assertEqual(status, 200)
        self.assertEqual(headers, dict(HEALTH_HEADERS))
        self.assertEqual(body, b'{"ready":false}')

    def test_open_health_surfaces_proven_connection_refused(self):
        refused = ConnectionRefusedError(61, "synthetic refusal")
        opener = _FakeOpener(error=URLError(refused))
        with mock.patch.object(entry, "_build_opener", return_value=opener):
            with self.assertRaises(ConnectionRefusedError):
                entry._open_health(8787)

    def test_open_health_does_not_treat_other_oserror_as_refusal(self):
        for label, error in {
            "timeout": URLError(socket.timeout("synthetic timeout")),
            "reset": URLError(ConnectionResetError(54, "synthetic reset")),
            "generic": URLError(OSError("synthetic")),
        }.items():
            with self.subTest(case=label):
                opener = _FakeOpener(error=error)
                with mock.patch.object(entry, "_build_opener", return_value=opener):
                    with self.assertRaises(URLError):
                        entry._open_health(8787)

    def test_open_health_returns_http_error_parts_for_503(self):
        error = HTTPError(
            "http://127.0.0.1:8787/healthz",
            503,
            "service unavailable",
            dict(HEALTH_HEADERS),
            io.BytesIO(b'{"ready":false}'),
        )
        opener = _FakeOpener(error=error)
        with mock.patch.object(entry, "_build_opener", return_value=opener):
            status, headers, body = entry._open_health(8787)
        self.assertEqual(status, 503)
        self.assertEqual(body, b'{"ready":false}')
        self.assertEqual(headers.get("Content-Type"), "application/json;charset=utf8")

    def test_read_health_parses_through_open_health(self):
        with mock.patch.object(
            entry,
            "_open_health",
            return_value=(200, health_headers(), b'{"ready":true,"reason":"ok"}'),
        ):
            status, parsed = entry._read_health(8787)
        self.assertEqual(status, 200)
        self.assertEqual(parsed, {"ready": True, "reason": "ok"})


# --------------------------------------------------------------------------- #
# launch: active / mismatched / unready / malformed
# --------------------------------------------------------------------------- #
@requires_python
class LaunchActiveTests(EntryFixtureTestCase):
    def test_active_ready_opens_the_fixed_url(self):
        config, payload = self.build_ready_payload()
        fetch = mock.Mock(return_value=(200, payload))
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=fetch, openb=openb)
        self.assertEqual(result, {"status": "opened", "reason": "ok"})
        openb.assert_called_once_with("http://127.0.0.1:8787/")
        fetch.assert_called_once_with(8787)

    def test_mismatched_board_digest_does_not_open(self):
        config, payload = self.build_ready_payload()
        tampered = dict(payload, boardSha256="0" * 64)
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=mock.Mock(return_value=(200, tampered)), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "identity_mismatch"})
        openb.assert_not_called()

    def test_mismatched_config_identity_does_not_open(self):
        config, payload = self.build_ready_payload()
        tampered = dict(payload, configIdentity="1" * 64)
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=mock.Mock(return_value=(200, tampered)), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "identity_mismatch"})
        openb.assert_not_called()

    def test_unready_response_does_not_open(self):
        config, payload = self.build_ready_payload()
        unready = dict(payload, ready=False, reason="catalog_changed")
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=mock.Mock(return_value=(503, unready)), openb=openb)
        self.assertEqual(result, {"status": "not_ready", "reason": "health_unready"})
        openb.assert_not_called()

    def test_wrong_service_is_malformed(self):
        config, payload = self.build_ready_payload()
        bad = dict(payload, service="other-service")
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=mock.Mock(return_value=(200, bad)), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "malformed_response"})
        openb.assert_not_called()

    def test_wrong_type_is_malformed(self):
        config, payload = self.build_ready_payload()
        bad = dict(payload, ready="true")
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=mock.Mock(return_value=(200, bad)), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "malformed_response"})
        openb.assert_not_called()

    def test_extra_field_is_malformed(self):
        config, payload = self.build_ready_payload()
        bad = dict(payload, surprise=1)
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=mock.Mock(return_value=(200, bad)), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "malformed_response"})
        openb.assert_not_called()

    def test_status_ready_mismatch_is_rejected(self):
        config, payload = self.build_ready_payload()
        unready = dict(payload, ready=False, reason="catalog_changed")
        openb = mock.Mock(return_value=True)
        # 200 with a not-ready payload, and 503 with a ready payload, both fail closed.
        for status, body in ((200, unready), (503, payload)):
            with self.subTest(status=status):
                result = self.launch(config, fetch=mock.Mock(return_value=(status, body)), openb=openb)
                self.assertEqual(result, {"status": "failed", "reason": "status_invalid"})
        openb.assert_not_called()

    def test_non_allowlisted_status_is_rejected(self):
        config, payload = self.build_ready_payload()
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=mock.Mock(return_value=(500, payload)), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "status_invalid"})
        openb.assert_not_called()

    def test_timeout_and_reset_are_generic_failures(self):
        config, _ = self.build_ready_payload()
        openb = mock.Mock(return_value=True)
        for label, error in {
            "timeout": socket.timeout("synthetic timeout"),
            "reset": ConnectionResetError(54, "synthetic reset"),
        }.items():
            with self.subTest(case=label):
                result = self.launch(config, fetch=mock.Mock(side_effect=error), openb=openb)
                self.assertEqual(result, {"status": "failed", "reason": "transport_failed"})
        openb.assert_not_called()

    def test_result_never_exposes_body_exception_or_path(self):
        config, payload = self.build_ready_payload()
        result = self.launch(config, fetch=mock.Mock(return_value=(200, dict(payload, service="x"))))
        self.assertEqual(set(result), {"status", "reason"})
        self.assertNotIn(str(self.root), json.dumps(result))
        self.assertNotIn("/", result["reason"])


# --------------------------------------------------------------------------- #
# launch through the *default* transport (opener patched; no socket)
# --------------------------------------------------------------------------- #
@requires_python
class LaunchDefaultTransportTests(EntryFixtureTestCase):
    def _raw(self, config, parts, *, openb=None):
        with mock.patch.object(entry, "_open_health", return_value=parts):
            return self.raw_launch(config, fetch=None, openb=openb if openb is not None else mock.Mock(return_value=True))

    def test_oversize_stops_without_open(self):
        self.prepare_ready()
        openb = mock.Mock(return_value=True)
        result = self._raw(
            self.config(),
            (200, health_headers(), b"x" * (entry.MAX_RESPONSE_BYTES + 1)),
            openb=openb,
        )
        self.assertEqual(result, {"status": "failed", "reason": "oversize"})
        openb.assert_not_called()

    def test_non_utf8_stops_without_open(self):
        self.prepare_ready()
        openb = mock.Mock(return_value=True)
        result = self._raw(self.config(), (200, health_headers(), b'{"a":"\xff"}'), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "malformed_response"})
        openb.assert_not_called()

    def test_header_mismatch_stops_without_open(self):
        self.prepare_ready()
        openb = mock.Mock(return_value=True)
        result = self._raw(
            self.config(), (200, health_headers(**{"Content-Type": "text/html"}), b"{}"), openb=openb
        )
        self.assertEqual(result, {"status": "failed", "reason": "header_invalid"})
        openb.assert_not_called()

    def test_redirect_status_stops_without_open(self):
        self.prepare_ready()
        openb = mock.Mock(return_value=True)
        result = self._raw(self.config(), (302, health_headers(), b"{}"), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "status_invalid"})
        openb.assert_not_called()

    def test_proven_refusal_through_default_transport_reaches_setup_required(self):
        self.prepare_ready()
        installed = mock.Mock(return_value=False)
        kick = mock.Mock()
        openb = mock.Mock(return_value=True)
        with mock.patch.object(entry, "_open_health", side_effect=ConnectionRefusedError(61, "no")):
            result = self.raw_launch(
                self.config(), fetch=None, installed=installed, kick=kick, openb=openb
            )
        self.assertEqual(result, {"status": "setup_required", "reason": "service_unregistered"})
        installed.assert_called_once()
        kick.assert_not_called()
        openb.assert_not_called()

    def test_default_transport_timeout_is_not_a_refusal(self):
        self.prepare_ready()
        installed = mock.Mock(return_value=False)
        with mock.patch.object(entry, "_open_health", side_effect=socket.timeout("t")):
            result = self.raw_launch(self.config(), fetch=None, installed=installed)
        self.assertEqual(result, {"status": "failed", "reason": "transport_failed"})
        installed.assert_not_called()


# --------------------------------------------------------------------------- #
# launch: refusal path
# --------------------------------------------------------------------------- #
@requires_python
class LaunchRefusalTests(EntryFixtureTestCase):
    def test_unregistered_service_is_setup_required_and_never_starts(self):
        config, _ = self.build_ready_payload()
        installed = mock.Mock(return_value=False)
        kick = mock.Mock()
        openb = mock.Mock(return_value=True)
        fetch = mock.Mock(side_effect=ConnectionRefusedError(61, "refused"))
        result = self.launch(config, fetch=fetch, installed=installed, kick=kick, openb=openb)
        self.assertEqual(result, {"status": "setup_required", "reason": "service_unregistered"})
        installed.assert_called_once()
        kick.assert_not_called()
        openb.assert_not_called()

    def test_installed_then_ready_opens_and_kicks_exactly_once(self):
        config, payload = self.build_ready_payload()
        responses = [ConnectionRefusedError(61, "refused"), (200, payload)]

        def fetch(port):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        installed = mock.Mock(return_value=True)
        kick = mock.Mock(return_value=True)
        openb = mock.Mock(return_value=True)
        sleeper = mock.Mock()
        result = self.launch(
            config, fetch=fetch, installed=installed, kick=kick, openb=openb, sleep=sleeper
        )
        self.assertEqual(result, {"status": "opened", "reason": "ok"})
        installed.assert_called_once()
        kick.assert_called_once_with()
        sleeper.assert_called_once_with(entry.PROBE_SLEEP_SECONDS)
        openb.assert_called_once_with("http://127.0.0.1:8787/")

    def test_second_refusal_never_kicks_again(self):
        config, payload = self.build_ready_payload()
        responses = [ConnectionRefusedError(61, "r1"), ConnectionRefusedError(61, "r2"), (200, payload)]

        def fetch(port):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        kick = mock.Mock(return_value=True)
        sleeper = mock.Mock()
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=fetch, kick=kick, openb=openb, sleep=sleeper)
        self.assertEqual(result, {"status": "opened", "reason": "ok"})
        kick.assert_called_once()
        self.assertEqual(sleeper.call_count, 2)

    def test_poll_exhausted_after_finite_attempts(self):
        config, _ = self.build_ready_payload()
        fetch = mock.Mock(side_effect=ConnectionRefusedError(61, "refused"))
        kick = mock.Mock(return_value=True)
        sleeper = mock.Mock()
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=fetch, kick=kick, openb=openb, sleep=sleeper)
        self.assertEqual(result, {"status": "failed", "reason": "probe_exhausted"})
        kick.assert_called_once()
        self.assertEqual(sleeper.call_count, entry.PROBE_ATTEMPTS)
        openb.assert_not_called()

    def test_malformed_response_after_kick_stops_immediately(self):
        config, payload = self.build_ready_payload()
        bad = dict(payload, service="other")
        responses = [ConnectionRefusedError(61, "refused"), (200, bad)]

        def fetch(port):
            item = responses.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

        kick = mock.Mock(return_value=True)
        sleeper = mock.Mock()
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=fetch, kick=kick, openb=openb, sleep=sleeper)
        self.assertEqual(result, {"status": "failed", "reason": "malformed_response"})
        kick.assert_called_once()
        self.assertEqual(sleeper.call_count, 1)
        openb.assert_not_called()

    def test_kickstart_returning_false_is_start_failed(self):
        config, _ = self.build_ready_payload()
        fetch = mock.Mock(side_effect=ConnectionRefusedError(61, "refused"))
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=fetch, kick=mock.Mock(return_value=False), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "start_failed"})
        openb.assert_not_called()

    def test_kickstart_raising_is_callback_failed(self):
        config, _ = self.build_ready_payload()
        fetch = mock.Mock(side_effect=ConnectionRefusedError(61, "refused"))
        openb = mock.Mock(return_value=True)
        result = self.launch(config, fetch=fetch, kick=mock.Mock(side_effect=RuntimeError("boom")), openb=openb)
        self.assertEqual(result, {"status": "failed", "reason": "callback_failed"})
        openb.assert_not_called()

    def test_service_probe_raising_is_callback_failed(self):
        config, _ = self.build_ready_payload()
        fetch = mock.Mock(side_effect=ConnectionRefusedError(61, "refused"))
        openb = mock.Mock(return_value=True)
        result = self.launch(
            config, fetch=fetch, installed=mock.Mock(side_effect=RuntimeError("boom")), openb=openb
        )
        self.assertEqual(result, {"status": "failed", "reason": "callback_failed"})
        openb.assert_not_called()

    def test_browser_failure_is_generic(self):
        config, payload = self.build_ready_payload()
        fetch = mock.Mock(return_value=(200, payload))
        for label, openb in {
            "false": mock.Mock(return_value=False),
            "raising": mock.Mock(side_effect=RuntimeError("boom")),
        }.items():
            with self.subTest(case=label):
                result = self.launch(config, fetch=fetch, openb=openb)
                self.assertEqual(result, {"status": "failed", "reason": "open_failed"})
                openb.assert_called_once_with("http://127.0.0.1:8787/")


# --------------------------------------------------------------------------- #
# launch: no action before validation / readiness
# --------------------------------------------------------------------------- #
@requires_python
class LaunchPreconditionTests(EntryFixtureTestCase):
    def test_invalid_config_is_invalid_request_without_any_action(self):
        fetch = mock.Mock()
        result = entry.launch(
            {"only": "one-field"},
            allowed_root=str(self.root),
            expected_board_sha256=self.board_sha,
            fetch_health=fetch,
            service_installed=mock.Mock(return_value=True),
            kickstart=mock.Mock(return_value=True),
            open_browser=mock.Mock(return_value=True),
            sleep=mock.Mock(),
        )
        self.assertEqual(result, {"status": "failed", "reason": "invalid_request"})
        fetch.assert_not_called()

    def test_no_configured_source_is_invalid_request(self):
        fetch = mock.Mock()
        result = entry.launch(
            self.config(dbPath=None, catalogPath=None),
            allowed_root=str(self.root),
            expected_board_sha256=self.board_sha,
            fetch_health=fetch,
            service_installed=mock.Mock(return_value=True),
            kickstart=mock.Mock(return_value=True),
            open_browser=mock.Mock(return_value=True),
            sleep=mock.Mock(),
        )
        self.assertEqual(result, {"status": "failed", "reason": "invalid_request"})
        fetch.assert_not_called()

    def test_changed_board_sha_stops_before_network(self):
        config, _ = self.build_ready_payload()
        self.board_script.write_text("changed board content\n", encoding="utf-8")
        fetch = mock.Mock()
        installed = mock.Mock(return_value=True)
        result = self.launch(config, fetch=fetch, installed=installed)
        self.assertEqual(result, {"status": "failed", "reason": "invalid_request"})
        fetch.assert_not_called()
        installed.assert_not_called()

    def test_unready_local_sources_stop_before_network(self):
        self.write_database(schema=False)
        self.write_catalog()
        fetch = mock.Mock()
        installed = mock.Mock(return_value=True)
        kick = mock.Mock(return_value=True)
        openb = mock.Mock(return_value=True)
        result = self.launch(
            self.config(), fetch=fetch, installed=installed, kick=kick, openb=openb
        )
        self.assertEqual(result, {"status": "not_ready", "reason": "source_not_ready"})
        fetch.assert_not_called()
        installed.assert_not_called()
        kick.assert_not_called()
        openb.assert_not_called()


# --------------------------------------------------------------------------- #
# Default process adapters (subprocess mocked; nothing is executed)
# --------------------------------------------------------------------------- #
@unittest.skipUnless(hasattr(os, "getuid"), "os.getuid is not available on this host")
class DefaultProcessTests(unittest.TestCase):
    def _run(self, *, returncode=0, raises=None):
        run = mock.Mock()
        if raises is not None:
            run.side_effect = raises
        else:
            run.return_value = mock.Mock(returncode=returncode)
        return run

    def _assert_bounded_kwargs(self, run, timeout):
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["timeout"], timeout)
        self.assertIs(kwargs["shell"], False)
        self.assertIs(kwargs["check"], False)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)

    def test_service_installed_uses_exact_print_argv(self):
        run = self._run(returncode=0)
        with mock.patch.object(entry.subprocess, "run", run):
            self.assertTrue(entry._default_service_installed())
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            ["/bin/launchctl", "print", "gui/%d/com.conductor.d4.board" % os.getuid()],
        )
        self._assert_bounded_kwargs(run, entry.SERVICE_TIMEOUT_SECONDS)

    def test_service_installed_false_on_nonzero_exit(self):
        run = self._run(returncode=113)
        with mock.patch.object(entry.subprocess, "run", run):
            self.assertFalse(entry._default_service_installed())

    def test_kickstart_argv_has_no_k_and_no_bootstrap_or_install(self):
        run = self._run(returncode=0)
        with mock.patch.object(entry.subprocess, "run", run):
            self.assertTrue(entry._default_kickstart())
        argv = run.call_args.args[0]
        self.assertEqual(
            argv,
            ["/bin/launchctl", "kickstart", "gui/%d/com.conductor.d4.board" % os.getuid()],
        )
        self.assertNotIn("-k", argv)
        self.assertNotIn("bootstrap", argv)
        self.assertNotIn("install", argv)
        self._assert_bounded_kwargs(run, entry.KICKSTART_TIMEOUT_SECONDS)

    def test_kickstart_budget_covers_fresh_launchd_throttle(self):
        def throttled_run(argv, **kwargs):
            if kwargs["timeout"] <= entry._APP_CONFIG.THROTTLE_INTERVAL:
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return mock.Mock(returncode=0)
        with mock.patch.object(entry.subprocess, "run", side_effect=throttled_run):
            self.assertTrue(entry._default_kickstart())
        self.assertEqual(entry.SERVICE_TIMEOUT_SECONDS, 5.0)
        self.assertLessEqual(entry.KICKSTART_TIMEOUT_SECONDS, 20.0)

    def test_open_browser_uses_exact_open_argv(self):
        run = self._run(returncode=0)
        with mock.patch.object(entry.subprocess, "run", run):
            self.assertTrue(entry._default_open_browser("http://127.0.0.1:8787/"))
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/usr/bin/open", "http://127.0.0.1:8787/"])
        self._assert_bounded_kwargs(run, entry.OPEN_TIMEOUT_SECONDS)

    def test_process_failures_return_false(self):
        run = self._run(raises=OSError("synthetic spawn failure"))
        with mock.patch.object(entry.subprocess, "run", run):
            self.assertFalse(entry._default_service_installed())
            self.assertFalse(entry._default_kickstart())
            self.assertFalse(entry._default_open_browser("http://127.0.0.1:8787/"))


# --------------------------------------------------------------------------- #
# Config reader + CLI
# --------------------------------------------------------------------------- #
class ConfigReaderTests(EntryFixtureTestCase):
    def test_reads_exact_bytes_of_a_regular_file(self):
        path = self.root / "config.json"
        raw = b'{"a":1}'
        path.write_bytes(raw)
        self.assertEqual(entry._read_config_bytes(path), raw)

    def test_rejects_oversize_config(self):
        path = self.root / "big.json"
        path.write_bytes(b"x" * (entry.MAX_CONFIG_BYTES + 1))
        with self.assertRaises(OSError):
            entry._read_config_bytes(path)

    def test_rejects_symlink_config(self):
        target = self.root / "real.json"
        target.write_bytes(b"{}")
        link = self.root / "link.json"
        os.symlink(str(target), str(link))
        with self.assertRaises(OSError):
            entry._read_config_bytes(link)

    def test_rejects_non_regular_config(self):
        with self.assertRaises(OSError):
            entry._read_config_bytes(self.root)


@requires_python
class CliTests(EntryFixtureTestCase):
    def _write_config(self, config=None):
        raw = json.dumps(config if config is not None else self.config(), ensure_ascii=False, sort_keys=True).encode("utf-8")
        path = self.root / "config.json"
        path.write_bytes(raw)
        return path, raw, hashlib.sha256(raw).hexdigest()

    def _argv(self, path, digest, board_sha=None):
        return [
            "--config",
            str(path),
            "--allowed-root",
            str(self.root),
            "--expected-board-sha256",
            board_sha if board_sha is not None else self.board_sha,
            "--expected-config-sha256",
            digest,
        ]

    def _run(self, argv):
        with contextlib.redirect_stderr(io.StringIO()):
            return entry.main(argv)

    def test_returns_zero_only_on_open(self):
        path, _raw, digest = self._write_config()
        with mock.patch.object(entry, "launch", return_value={"status": "opened", "reason": "ok"}) as launch_mock:
            code = self._run(self._argv(path, digest))
        self.assertEqual(code, 0)
        launch_mock.assert_called_once()
        call_args, call_kwargs = launch_mock.call_args
        self.assertEqual(call_args[0], self.config())
        self.assertEqual(call_kwargs["allowed_root"], str(self.root))
        self.assertEqual(call_kwargs["expected_board_sha256"], self.board_sha)

    def test_non_open_result_exits_nonzero(self):
        path, _raw, digest = self._write_config()
        with mock.patch.object(entry, "launch", return_value={"status": "failed", "reason": "invalid_request"}):
            code = self._run(self._argv(path, digest))
        self.assertEqual(code, 1)

    def test_digest_mismatch_never_launches(self):
        path, _raw, _digest = self._write_config()
        with mock.patch.object(entry, "launch") as launch_mock:
            code = self._run(self._argv(path, "0" * 64))
        self.assertEqual(code, 1)
        launch_mock.assert_not_called()

    def test_tampered_config_never_launches(self):
        path, _raw, digest = self._write_config()
        # Digest is computed over the *original* bytes, then the file is replaced.
        path.write_bytes(b'{"other":1}')
        with mock.patch.object(entry, "launch") as launch_mock:
            code = self._run(self._argv(path, digest))
        self.assertEqual(code, 1)
        launch_mock.assert_not_called()

    def test_duplicate_keys_config_never_launches(self):
        raw = b'{"port":1,"port":2}'
        path = self.root / "dup.json"
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        with mock.patch.object(entry, "launch") as launch_mock:
            code = self._run(self._argv(path, digest))
        self.assertEqual(code, 1)
        launch_mock.assert_not_called()

    def test_oversize_config_never_launches(self):
        raw = b"x" * (entry.MAX_CONFIG_BYTES + 1)
        path = self.root / "big.json"
        path.write_bytes(raw)
        digest = hashlib.sha256(raw).hexdigest()
        with mock.patch.object(entry, "launch") as launch_mock:
            code = self._run(self._argv(path, digest))
        self.assertEqual(code, 1)
        launch_mock.assert_not_called()

    def test_symlink_config_never_launches(self):
        target, _raw, digest = self._write_config()
        link = self.root / "link.json"
        os.symlink(str(target), str(link))
        with mock.patch.object(entry, "launch") as launch_mock:
            code = self._run(self._argv(link, digest))
        self.assertEqual(code, 1)
        launch_mock.assert_not_called()

    def test_missing_config_never_launches(self):
        with mock.patch.object(entry, "launch") as launch_mock:
            code = self._run(self._argv(self.root / "missing.json", "0" * 64))
        self.assertEqual(code, 1)
        launch_mock.assert_not_called()

    def test_missing_required_arguments_exit(self):
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                entry.main(["--config", str(self.root / "config.json")])


if __name__ == "__main__":
    unittest.main()
