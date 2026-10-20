"""Local read-only D4 board backed by the existing task database.

G2-B (candidate, still local-only): the request handler additionally supports a configured
``GET /healthz`` when ``make_handler`` is called with the keyword-only
``health_config`` + ``expected_board_sha256`` pair, and ``main`` optionally accepts
``--runtime-dir`` for the fixed-configuration path. The legacy call shapes (no health
configuration, ``--port 0``, the static assets, the read-only JSON API and the
read-only 405 write rejection) are unchanged.

The configured health identity is frozen exactly once, in ``make_handler``, before the
handler class exists and long before any socket is bound; every later request re-measures
the frozen inputs read-only through the frozen G2-A runtime, so a source change or a
replacement is reported without any re-freeze. Importing this module performs no I/O, no
install, no service load/start/stop and no bind; it never creates, migrates or writes a
runtime/logs/db/catalog path.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import stat
import sys
from collections.abc import Mapping
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from contracts.schemas import ContractError  # noqa: E402
from projections.read_models import ReadModels  # noqa: E402
from projections.project_catalog import ProjectCatalog  # noqa: E402
from storage.sqlite_store import SqliteStore  # noqa: E402

ASSETS = Path(__file__).resolve().parents[1] / "board"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/board.css": ("board.css", "text/css; charset=utf-8"),
          "/board.js": ("board.js", "text/javascript; charset=utf-8"),
          "/project-view.js": ("project-view.js", "text/javascript; charset=utf-8")}

HEALTH_ROUTE = "/healthz"
HEALTH_CONTENT_TYPE = "application/json;charset=utf8"
HEALTH_UNCONFIGURED_BODY = b'{"error":"health_unconfigured"}'
HEALTH_READ_FAILED_BODY = b'{"error":"health_read_failed"}'
_READ_CHUNK = 65536


def _load_script_module(name, filename):
    """Load one preplaced sibling script by its exact file path (no directory scan)."""
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load %s" % filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# The frozen G2-A reader/runtime and the frozen G1 config validator, reused unchanged and
# never re-implemented here. Loading them by exact path is side-effect free (no I/O, no
# process, no service, no socket).
_READER = _load_script_module("d4_health_reader", "d4_health_reader.py")
_HEALTH = _load_script_module("d4_health_runtime", "d4_health_runtime.py")
_APP_CONFIG = _load_script_module("d4_app_config", "d4_app_config.py")


def board_data(db_path, route, catalog_path=None):
    catalog = ProjectCatalog(catalog_path) if catalog_path else None
    if catalog:
        registered = catalog.route(route)
        if registered is not None:
            return registered
    if db_path is None:
        if route == "/api/projects":
            return {"projects": catalog.projects() if catalog else []}
        if route == "/api/reviews":
            return {"items": []}
        raise KeyError(route)
    store = SqliteStore(db_path, read_only=True)
    try:
        store.conn.execute("BEGIN")
        models = ReadModels(store)
        if route == "/api/projects":
            projects = models.projects()["projects"]
            if catalog:
                if any(p["projectId"] in catalog.project_ids for p in projects):
                    raise ValueError("catalog_live_project_collision")
                projects = catalog.projects() + projects
            return {"projects": projects}
        if route == "/api/reviews":
            return models.review_queue()
        if route.startswith("/api/projects/"):
            return models.project_summary(unquote(route[len("/api/projects/"):]))
        if route.startswith("/api/tasks/"):
            return models.task_detail(unquote(route[len("/api/tasks/"):]))
        raise KeyError(route)
    finally:
        store.close()


def _same_source(left, right):
    """Compare two configured source paths lexically (``None`` stays ``None``)."""
    return (None if left is None else str(left)) == (None if right is None else str(right))


def _health_config_sources(health_config):
    """Return the configured ``(dbPath, catalogPath)`` pair of an AppConfig or mapping."""
    if isinstance(health_config, Mapping):
        try:
            return health_config["dbPath"], health_config["catalogPath"]
        except KeyError:
            raise ValueError("health_config must carry the exact nine G1 config fields") from None
    missing = object()
    db_value = getattr(health_config, "db_path", missing)
    catalog_value = getattr(health_config, "catalog_path", missing)
    if db_value is missing or catalog_value is missing:
        raise ValueError("health_config must be a G1 AppConfig or a nine-field mapping")
    return db_value, catalog_value


def _require_matching_health_sources(health_config, db_path, catalog_path):
    """Fail closed when the health configuration does not describe this handler's sources."""
    config_db, config_catalog = _health_config_sources(health_config)
    if not _same_source(config_db, db_path) or not _same_source(config_catalog, catalog_path):
        raise ValueError("health_config db/catalog must exactly match the handler sources")


def make_handler(db_path, token=None, catalog_path=None, *, health_config=None,
                 expected_board_sha256=None):
    """Build the request handler class (legacy call shape preserved).

    ``health_config`` and ``expected_board_sha256`` are keyword-only and must be given
    together: exactly one of them is rejected. When both are given, the configured health
    identity is frozen **once here** -- before the handler class is created and before any
    socket exists -- and ``GET /healthz`` then answers from that frozen identity.

    The nine configured fields must describe exactly this handler's ``db_path``/
    ``catalog_path``, otherwise ``ValueError`` is raised before anything is bound. A
    configuration that cannot be frozen raises the frozen runtime's
    ``HealthRuntimeError`` (one generic code) before anything is bound.
    """
    if (health_config is None) != (expected_board_sha256 is None):
        raise ValueError("health_config and expected_board_sha256 must be provided together")
    frozen_health = None
    if health_config is not None:
        _require_matching_health_sources(health_config, db_path, catalog_path)
        frozen_health = _HEALTH.freeze_identity(
            health_config, expected_board_sha256=expected_board_sha256
        )

    class Handler(BaseHTTPRequestHandler):
        def _send(self, status, content_type, body):
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body)

        def _send_health_error(self, body):
            self._send(503, HEALTH_CONTENT_TYPE, body)

        def _serve_health(self):
            if frozen_health is None:
                self._send_health_error(HEALTH_UNCONFIGURED_BODY)
                return
            try:
                payload = _HEALTH.collect_health(frozen_health)
                body = _READER.serialize_health_payload(payload)
            except Exception:
                self.log_error("D4 health collection failed")
                self._send_health_error(HEALTH_READ_FAILED_BODY)
                return
            self._send(200 if payload["ready"] is True else 503, HEALTH_CONTENT_TYPE, body)

        def do_GET(self):
            route = urlsplit(self.path).path
            if route == HEALTH_ROUTE:
                self._serve_health()
                return
            if route in STATIC:
                name, kind = STATIC[route]
                self._send(200, kind, (ASSETS / name).read_bytes())
                return
            if not route.startswith("/api/"):
                self._send(404, "application/json", b'{"error":"not_found"}')
                return
            try:
                payload = board_data(db_path, route, catalog_path)
                status = 200
            except (ContractError, KeyError):
                payload, status = {"error": "not_found"}, 404
            except Exception:
                self.log_error("D4 database read failed")
                payload, status = {"error": "read_failed"}, 503
            self._send(status, "application/json; charset=utf-8",
                       json.dumps(payload, ensure_ascii=False).encode("utf-8"))

        def do_POST(self):
            self._send(405, "application/json", b'{"error":"read_only"}')

        do_PUT = do_POST
        do_PATCH = do_POST
        do_DELETE = do_POST

    return Handler


def _script_digest_bounded(path):
    """sha256 of the actual board script, read once within the frozen byte bound."""
    limit = _HEALTH.MAX_BOARD_SCRIPT_BYTES
    info = os.lstat(path)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("board script must be a bounded regular file")
    digest = hashlib.sha256()
    total = 0
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ValueError("board script must be a bounded regular file")
            digest.update(chunk)
    return digest.hexdigest()


def _derive_allowed_root(fields):
    """Derived common root of the configured paths, for CLI filesystem validation only.

    This is the lexical containment root handed to the frozen G1 ``validate_config`` for
    the ``--runtime-dir`` CLI path. It is neither an authorization nor an authentication
    and grants nothing by itself; the frozen validator still enforces every per-path rule.
    """
    candidates = [fields["boardScript"], fields["runtimeDir"]]
    for name in ("dbPath", "catalogPath"):
        if fields[name] is not None:
            candidates.append(fields[name])
    return os.path.commonpath([os.path.abspath(path) for path in candidates])


def _fixed_config_handler(args):
    """Build the configured health handler for the optional ``--runtime-dir`` mode.

    The nine fixed fields are built from the actual CLI arguments, the fixed configured
    launcher and this script's actual path -- never from the environment and never from
    free argv. The frozen G1 ``validate_config`` then enforces the fixed host, the fixed
    1024..65535 port, the ``/usr/bin/python3`` configured launch path, the fixed label, the
    canonical non-temporary existing runtime/logs directories, the explicit db/catalog
    sources and the actual ``__file__`` content digest. Nothing is created here:
    runtime/logs/db/catalog must already exist.
    """
    # Apple /usr/bin/python3 expands sys.executable to CommandLineTools.
    # Config identity binds the approved invocation path; it is not runtime authentication.
    board_script = str(Path(__file__).resolve())
    fields = {
        "schemaVersion": _APP_CONFIG.SCHEMA_VERSION,
        "host": _APP_CONFIG.ALLOWED_HOST,
        "port": args.port,
        "dbPath": None if args.db is None else str(args.db),
        "catalogPath": None if args.catalog is None else str(args.catalog),
        "pythonPath": _APP_CONFIG.ALLOWED_PYTHON_PATH,
        "boardScript": board_script,
        "runtimeDir": str(args.runtime_dir),
        "launchAgentLabel": _APP_CONFIG.LAUNCH_AGENT_LABEL,
    }
    expected_board_sha256 = _script_digest_bounded(board_script)
    config = _APP_CONFIG.validate_config(
        fields,
        allowed_root=_derive_allowed_root(fields),
        expected_board_sha256=expected_board_sha256,
    )
    return make_handler(
        args.db,
        catalog_path=args.catalog,
        health_config=config,
        expected_board_sha256=expected_board_sha256,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description="D4 本机只读任务看板")
    parser.add_argument("--db", type=Path, help="已有执行数据库，只读")
    parser.add_argument("--catalog", type=Path, help="只读项目资料登记文件")
    parser.add_argument("--runtime-dir", type=Path,
                        help="已存在的运行时目录，按固定配置启用 /healthz")
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    if args.db is None and args.catalog is None:
        parser.error("请指定 --db 或 --catalog")
    if args.db is not None and not args.db.is_file():
        parser.error("数据库文件不存在")
    if args.catalog:
        ProjectCatalog(args.catalog)
    if args.runtime_dir is None:
        handler = make_handler(args.db, catalog_path=args.catalog)
    else:
        handler = _fixed_config_handler(args)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print("url=http://127.0.0.1:%d" % server.server_port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
