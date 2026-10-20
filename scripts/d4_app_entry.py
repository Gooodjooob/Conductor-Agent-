"""D4 board local App entry candidate (G2-C, orchestrator only, read-only probe).

Scope
-----
This module is a *pure orchestrator* for the operator's local "open the D4 board"
action. It reuses the frozen G1 config validator, the frozen G2-A health runtime and
the frozen G2-B reader **unchanged** (loaded by exact sibling path) and adds only:

* ``launch(...)`` -- one bounded orchestration function that (1) validates the G1
  config, (2) freezes the expected startup identity and collects the expected source
  readiness, (3) probes the fixed loopback ``/healthz`` endpoint, and only when a
  matching, ready response is proven, (4) opens the fixed loopback URL. A proven
  ``ConnectionRefusedError`` is the *only* trigger that may consider starting the fixed
  launchd service.
* ``main(argv)`` -- the explicit CLI (``--config``, ``--allowed-root``,
  ``--expected-board-sha256``, ``--expected-config-sha256``) that reads the pinned
  config file, verifies its pinned digest and delegates to ``launch``.

Hard boundaries
---------------
* No install, no ``launchctl bootstrap``/``bootout``/``enable``, no service mutation and
  no migration. The refusal path may issue at most one ``/bin/launchctl kickstart`` of
  the fixed, already-installed label -- never ``-k`` and never ``bootstrap``/``install``.
* No free URL, label or argv and no environment/config discovery: the host is the fixed
  numeric loopback ``127.0.0.1``, the path is the fixed ``/healthz``, the port comes from
  the validated G1 config, and the launchd label is the fixed G1 label.
* The user result is a *bounded generic* dict ``{"status": ..., "reason": ...}`` whose
  values come from fixed sets: a response body, an exception, a message or a filesystem
  path can never reach it.
* Endpoint responses are untrusted: the response is read with a bounded read
  (``MAX_RESPONSE_BYTES + 1``), the status/headers are validated explicitly, the body is
  decoded as strict UTF-8 and parsed as strict JSON (duplicate keys and non-finite
  numbers rejected), and the resulting payload must pass the frozen reader's exact
  eight-field validation and match the frozen board/config/data-source identities.
* ``auth="none"`` in the frozen payload is *not* authentication: the identity comparison
  is wrong-instance / misconfiguration detection only and grants no trust.
* Importing this module starts nothing, opens no socket, touches no database, performs no
  install/service change, and has no side effect beyond loading the frozen sibling
  modules by exact path.

Honest limitations (not claimed as covered)
-------------------------------------------
* This is not a complete MVP: it does not install, migrate, supervise or verify the
  service beyond the single fixed probe sequence described above.
* The bounded probe is best-effort within ``PROBE_ATTEMPTS`` finite attempts; a service
  that needs longer to become ready yields a generic ``failed``/``probe_exhausted``
  result and never an unbounded wait.
"""

from __future__ import annotations

import argparse
import codecs
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import time
import urllib.request
from collections import namedtuple
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.error import HTTPError, URLError

__all__ = ["launch", "main"]

# --------------------------------------------------------------------------- #
# Frozen sibling modules (loaded by exact path, side-effect free)
# --------------------------------------------------------------------------- #
READ_CHUNK = 65536


def _load_script_module(name: str, filename: str):
    """Load one preplaced sibling script by its exact file path (no directory scan).

    ``sys.modules`` is populated *before* ``exec_module`` so that any module-level
    dataclass defined by the loaded module (``AppConfig``, ``FrozenIdentity``) resolves
    its ``__module__`` through a registered module entry.
    """
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load %s" % filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_APP_CONFIG = _load_script_module("d4_app_config", "d4_app_config.py")
_READER = _load_script_module("d4_health_reader", "d4_health_reader.py")
_HEALTH = _load_script_module("d4_health_runtime", "d4_health_runtime.py")


# --------------------------------------------------------------------------- #
# Fixed literals
# --------------------------------------------------------------------------- #
HEALTH_HOST = "127.0.0.1"
HEALTH_PATH = "/healthz"
HEALTH_TIMEOUT = 1.0
MAX_RESPONSE_BYTES = 16 * 1024

LAUNCHCTL_PATH = "/bin/launchctl"
OPEN_PATH = "/usr/bin/open"
LAUNCH_AGENT_LABEL = _APP_CONFIG.LAUNCH_AGENT_LABEL

SERVICE_TIMEOUT_SECONDS = 5.0
# A fresh launchd spawn can defer kickstart until its fixed throttle expires.
KICKSTART_TIMEOUT_SECONDS = float(_APP_CONFIG.THROTTLE_INTERVAL) + 5.0
OPEN_TIMEOUT_SECONDS = 5.0
PROBE_ATTEMPTS = 10
PROBE_SLEEP_SECONDS = 0.2

MAX_CONFIG_BYTES = 16 * 1024

VALID_STATUSES = (200, 503)

# Bounded result vocabularies: the only values that can ever reach the caller.
RESULT_STATUSES = frozenset({"opened", "setup_required", "not_ready", "failed"})
RESULT_REASONS = frozenset({
    "ok",
    "invalid_request",
    "source_not_ready",
    "service_unregistered",
    "start_failed",
    "probe_exhausted",
    "identity_mismatch",
    "health_unready",
    "malformed_response",
    "header_invalid",
    "oversize",
    "status_invalid",
    "transport_failed",
    "open_failed",
    "callback_failed",
})

# Bounded CLI diagnostic codes (generic, never a path, body or exception text).
CLI_REASONS = frozenset({
    "config_unreadable",
    "config_digest_mismatch",
    "config_invalid",
})

IDENTITY_MISMATCH_REASONS = frozenset({
    "board_mismatch",
    "config_mismatch",
    "data_source_mismatch",
    "invalid_payload",
})

_OUTCOME_KINDS = ("ready", "refused", "mismatch", "unready", "malformed", "invalid", "unavailable")

_Outcome = namedtuple("_Outcome", "kind reason")

_LOWER_HEX = frozenset("0123456789abcdef")


# --------------------------------------------------------------------------- #
# Internal error types / bounded result
# --------------------------------------------------------------------------- #
class _HealthFailure(Exception):
    """Generic transport/parse failure carrying exactly one bounded reason code."""

    def __init__(self, code: str) -> None:
        if code not in RESULT_REASONS:
            raise ValueError("invalid health failure code")
        self.code = code
        super().__init__(code)


def _result(status: str, reason: str) -> Dict[str, str]:
    """Build the bounded generic result; unreachable for an out-of-set value."""
    if status not in RESULT_STATUSES or reason not in RESULT_REASONS:
        raise ValueError("invalid result")
    return {"status": status, "reason": reason}


def _is_lower_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _LOWER_HEX for char in value)


# --------------------------------------------------------------------------- #
# Strict response parsing (pure, no I/O)
# --------------------------------------------------------------------------- #
def _reject_duplicate_keys(pairs):
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError("duplicate key")
        seen[key] = value
    return seen


def _reject_non_finite(name):
    raise ValueError("non-finite number")


def _strict_json_object(text: str) -> Any:
    """Parse strict JSON: duplicate keys and non-finite numbers are rejected."""
    try:
        return json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (ValueError, TypeError, RecursionError):
        raise _HealthFailure("malformed_response") from None


def _is_json_utf8(value: Any) -> bool:
    """True when a Content-Type value is ``application/json`` with a UTF-8 charset.

    The spelling is normalized through ``codecs`` so the endpoint's
    ``application/json;charset=utf8`` and a canonical ``...; charset=UTF-8`` both pass.
    """
    if not isinstance(value, str):
        return False
    segments = [segment.strip() for segment in value.split(";")]
    if not segments or segments[0].lower() != "application/json":
        return False
    charset: Optional[str] = None
    for segment in segments[1:]:
        if segment.lower().startswith("charset="):
            charset = segment.split("=", 1)[1].strip().strip('"').strip("'")
    if charset is None:
        return False
    try:
        return codecs.lookup(charset).name == "utf-8"
    except LookupError:
        return False


def _headers_valid(headers: Any) -> bool:
    """Explicit header validation: JSON/UTF-8, ``no-store`` and ``nosniff``."""
    getter = getattr(headers, "get", None)
    if getter is None:
        return False
    if not _is_json_utf8(getter("Content-Type")):
        return False
    cache = getter("Cache-Control")
    if not isinstance(cache, str):
        return False
    if "no-store" not in [token.strip().lower() for token in cache.split(",")]:
        return False
    nosniff = getter("X-Content-Type-Options")
    if not isinstance(nosniff, str) or nosniff.strip().lower() != "nosniff":
        return False
    return True


def _parse_health_response(status: Any, headers: Any, body: Any):
    """Validate one raw response and return ``(status, parsed_json)``.

    Raises :class:`_HealthFailure` with a bounded code on any status/header/body
    mismatch, oversized body, non-UTF8 body, JSON duplicate key or non-finite number.
    """
    if status not in VALID_STATUSES:
        raise _HealthFailure("status_invalid")
    if not _headers_valid(headers):
        raise _HealthFailure("header_invalid")
    if not isinstance(body, (bytes, bytearray)):
        raise _HealthFailure("malformed_response")
    if len(body) > MAX_RESPONSE_BYTES:
        raise _HealthFailure("oversize")
    try:
        text = bytes(body).decode("utf-8")
    except UnicodeDecodeError:
        raise _HealthFailure("malformed_response") from None
    return status, _strict_json_object(text)


# --------------------------------------------------------------------------- #
# Default loopback transport (no redirects, empty proxy config)
# --------------------------------------------------------------------------- #
class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect: returning ``None`` makes urllib raise the 3xx error."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _build_opener():
    """Opener with redirects disabled and environment proxies ignored."""
    return urllib.request.build_opener(_NoRedirectHandler(), urllib.request.ProxyHandler({}))


def _open_health(port: int):
    """GET the fixed ``http://127.0.0.1:<port>/healthz`` and return raw parts.

    Returns ``(status, headers, body)`` with the body read at most
    ``MAX_RESPONSE_BYTES + 1`` bytes. A proven connection refusal is surfaced as a real
    ``ConnectionRefusedError``; every other transport problem propagates as an
    ``OSError``/``URLError`` (never as a refusal).
    """
    url = "http://%s:%d%s" % (HEALTH_HOST, port, HEALTH_PATH)
    request = urllib.request.Request(url, method="GET")
    opener = _build_opener()
    try:
        response = opener.open(request, timeout=HEALTH_TIMEOUT)
    except HTTPError as error:
        try:
            return error.code, error.headers, error.read(MAX_RESPONSE_BYTES + 1)
        finally:
            error.close()
    except URLError as error:
        if isinstance(error.reason, ConnectionRefusedError):
            raise ConnectionRefusedError("connection refused") from None
        raise
    try:
        return response.status, response.headers, response.read(MAX_RESPONSE_BYTES + 1)
    finally:
        response.close()


def _read_health(port: int):
    """Default health transport: open then strictly parse the fixed endpoint."""
    status, headers, body = _open_health(port)
    return _parse_health_response(status, headers, body)


# --------------------------------------------------------------------------- #
# Probe + orchestration helpers
# --------------------------------------------------------------------------- #
def _probe(fetch, port, expected_board, expected_config, expected_data):
    """Classify one probe into a bounded :class:`_Outcome`."""
    try:
        status, payload = fetch(port)
    except ConnectionRefusedError:
        return _Outcome("refused", "refused")
    except _HealthFailure as failure:
        return _Outcome("malformed", failure.code)
    except OSError:
        return _Outcome("unavailable", "transport_failed")
    except Exception:
        return _Outcome("unavailable", "transport_failed")

    if status not in VALID_STATUSES:
        return _Outcome("invalid", "status_invalid")
    try:
        validated = _READER.validate_health_payload(payload)
    except (ValueError, TypeError):
        return _Outcome("malformed", "malformed_response")
    if (status == 200) != (validated["ready"] is True):
        return _Outcome("invalid", "status_invalid")
    ready, reason = _READER.evaluate_health_payload(
        validated,
        expected_board_sha256=expected_board,
        expected_config_identity=expected_config,
        expected_data_source_identity=expected_data,
    )
    if ready:
        return _Outcome("ready", "ok")
    if reason in IDENTITY_MISMATCH_REASONS:
        return _Outcome("mismatch", "identity_mismatch")
    return _Outcome("unready", "health_unready")


def _failure_for(outcome) -> Dict[str, str]:
    """Map a non-ready, non-refused outcome to a bounded failure result."""
    if outcome.kind == "unready":
        return _result("not_ready", "health_unready")
    if outcome.kind == "mismatch":
        return _result("failed", "identity_mismatch")
    if outcome.reason in RESULT_REASONS:
        return _result("failed", outcome.reason)
    return _result("failed", "malformed_response")


def _call(callback):
    """Return ``bool(callback())`` or ``None`` when the callback raised."""
    try:
        return bool(callback())
    except Exception:
        return None


def _open(open_callback, port: int) -> Dict[str, str]:
    """Open the fixed loopback URL; any non-true/failed callback is a generic failure."""
    url = "http://%s:%d/" % (HEALTH_HOST, port)
    if _call(lambda: open_callback(url)) is True:
        return _result("opened", "ok")
    return _result("failed", "open_failed")


# --------------------------------------------------------------------------- #
# Default launcher / browser adapters (exact argv, no shell)
# --------------------------------------------------------------------------- #
def _service_target() -> str:
    return "gui/%d/%s" % (os.getuid(), LAUNCH_AGENT_LABEL)


def _run_command(argv, timeout: float) -> bool:
    """Run one exact argv with a bounded timeout, no shell; True only on exit 0."""
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            shell=False,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def _default_service_installed() -> bool:
    """``launchctl print gui/<uid>/com.conductor.d4.board`` -> registered?"""
    return _run_command([LAUNCHCTL_PATH, "print", _service_target()], SERVICE_TIMEOUT_SECONDS)


def _default_kickstart() -> bool:
    """One ``launchctl kickstart`` of the fixed label (never ``-k``, bootstrap or install)."""
    return _run_command([LAUNCHCTL_PATH, "kickstart", _service_target()], KICKSTART_TIMEOUT_SECONDS)


def _default_open_browser(url: str) -> bool:
    """``open`` the fixed loopback URL (exact argv, no shell)."""
    return _run_command([OPEN_PATH, url], OPEN_TIMEOUT_SECONDS)


# --------------------------------------------------------------------------- #
# Public orchestration
# --------------------------------------------------------------------------- #
def launch(
    config_mapping: Any,
    *,
    allowed_root: str,
    expected_board_sha256: str,
    fetch_health=None,
    service_installed=None,
    kickstart=None,
    open_browser=None,
    sleep=None,
) -> Dict[str, str]:
    """Validate, freeze, probe and -- only on a proven ready match -- open the board.

    Ordering is fixed and fail-closed: the G1 config is validated first, the expected
    startup identity and the expected source readiness are frozen/collected before any
    network/service/browser action, and only a proven ``ConnectionRefusedError`` may
    consider starting the fixed service. Any malformed response, header/status mismatch,
    timeout/reset, wrong service, digest mismatch or unready response produces no launch
    and no open action. The return value is always a bounded generic
    ``{"status": ..., "reason": ...}``.
    """
    fetch = _read_health if fetch_health is None else fetch_health
    installed_callback = _default_service_installed if service_installed is None else service_installed
    start_callback = _default_kickstart if kickstart is None else kickstart
    open_callback = _default_open_browser if open_browser is None else open_browser
    waiter = time.sleep if sleep is None else sleep

    # 1. G1 config validation (the config cannot name its own digest or its own root).
    try:
        validated = _APP_CONFIG.validate_config(
            config_mapping,
            allowed_root=allowed_root,
            expected_board_sha256=expected_board_sha256,
        )
    except (ValueError, TypeError, OSError):
        return _result("failed", "invalid_request")

    # 2. Freeze the expected startup identity and the expected source readiness first.
    try:
        frozen = _HEALTH.freeze_identity(validated, expected_board_sha256=expected_board_sha256)
    except _HEALTH.HealthRuntimeError:
        return _result("not_ready", "source_not_ready")
    try:
        expected = _HEALTH.collect_health(frozen)
    except _HEALTH.HealthRuntimeError:
        return _result("not_ready", "source_not_ready")
    if expected.get("ready") is not True:
        return _result("not_ready", "source_not_ready")

    port = validated.port
    expected_board = frozen.board_sha256
    expected_config = frozen.config_identity
    expected_data = frozen.data_source_identity

    # 3. First probe: no network/service/browser side effect beyond this bounded GET.
    first = _probe(fetch, port, expected_board, expected_config, expected_data)
    if first.kind == "ready":
        return _open(open_callback, port)
    if first.kind != "refused":
        return _failure_for(first)

    # 4. Refusal path: only a proven refusal reaches here.
    installed = _call(installed_callback)
    if installed is None:
        return _result("failed", "callback_failed")
    if not installed:
        return _result("setup_required", "service_unregistered")
    started = _call(start_callback)
    if started is None:
        return _result("failed", "callback_failed")
    if not started:
        return _result("failed", "start_failed")

    # The service was kicked once. A later refusal never kicks again: it only continues
    # the finite poll. Any malformed response (or any other non-refusal outcome) stops.
    for _ in range(PROBE_ATTEMPTS):
        try:
            waiter(PROBE_SLEEP_SECONDS)
        except Exception:
            return _result("failed", "callback_failed")
        outcome = _probe(fetch, port, expected_board, expected_config, expected_data)
        if outcome.kind == "ready":
            return _open(open_callback, port)
        if outcome.kind == "refused":
            continue
        return _failure_for(outcome)
    return _result("failed", "probe_exhausted")


# --------------------------------------------------------------------------- #
# CLI (explicit arguments; the packaged wrapper pins them)
# --------------------------------------------------------------------------- #
def _read_config_bytes(path: Any) -> bytes:
    """Read one regular, non-symlink config file, bounded to ``MAX_CONFIG_BYTES``."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(os.fspath(path), flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("config is not a regular file")
        chunks = []
        total = 0
        while True:
            chunk = os.read(descriptor, READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_CONFIG_BYTES:
                raise OSError("config too large")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_stderr(message: str) -> None:
    sys.stderr.write(message + "\n")


def _cli_failure(reason: str) -> int:
    if reason not in CLI_REASONS:
        reason = "config_invalid"
    _write_stderr("d4-app-entry: " + reason)
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="d4-app-entry", description="D4 board local app entry")
    parser.add_argument("--config", required=True)
    parser.add_argument("--allowed-root", required=True)
    parser.add_argument("--expected-board-sha256", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    return parser


def main(argv=None) -> int:
    """Explicit CLI entry point.

    The caller-controlled arguments are local user configuration, not an authentication
    boundary; the packaged wrapper is expected to pin them. The pinned config digest is
    verified before ``launch`` is called, and the exit code is 0 only on a successful
    open. Error messages are generic and carry no path, body or exception text.
    """
    args = _build_parser().parse_args(argv)

    try:
        raw = _read_config_bytes(args.config)
    except OSError:
        return _cli_failure("config_unreadable")

    if (
        not _is_lower_hex64(args.expected_config_sha256)
        or hashlib.sha256(raw).hexdigest() != args.expected_config_sha256
    ):
        return _cli_failure("config_digest_mismatch")

    try:
        mapping = _strict_json_object(raw.decode("utf-8"))
    except (ValueError, _HealthFailure):
        return _cli_failure("config_invalid")

    result = launch(
        mapping,
        allowed_root=args.allowed_root,
        expected_board_sha256=args.expected_board_sha256,
    )
    if result.get("status") == "opened":
        return 0
    _write_stderr("d4-app-entry: %s/%s" % (result.get("status"), result.get("reason")))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
