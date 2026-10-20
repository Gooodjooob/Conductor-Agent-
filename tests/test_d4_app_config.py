"""candidate/scripts/d4_app_config.py 的离线 unittest 套件（G1 合同）。

边界说明（与合同一致）：
* 只用标准库 unittest；无 pytest、无环境变量覆盖、无动态目录扫描；
* 不发起网络、不启动进程、不安装/加载服务、不触碰正式源码或正式数据库；
* fixture 只在 ``tests/test-artifacts`` 下自建并自清理，不使用系统默认临时目录；
* 网络/进程 trap 只是负面证据，不是硬隔离证明；
* worker 本轮 Bash 为空，未运行任何用例，全部结果待 cox 独立复跑。
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import hashlib
import os
import plistlib
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_CANDIDATE_ROOT = Path(os.path.abspath(__file__)).parents[1]
_SCRIPTS = _CANDIDATE_ROOT / "scripts"
_FIXTURES = Path(os.path.abspath(__file__)).parent / "test-artifacts"

if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import d4_app_config as appcfg  # noqa: E402

BOARD_FIXTURE_BYTES = b'"""fixture board script for the candidate test suite."""\nVALUE = 1\n'
DB_FIXTURE_BYTES = b"SQLite format 3\x00fixture-not-a-real-database"
CATALOG_FIXTURE_BYTES = b'{"projects": []}'
PYTHON_PATH = "/usr/bin/python3"
PYTHON_AVAILABLE = os.path.isfile(PYTHON_PATH) and os.access(PYTHON_PATH, os.X_OK)
requires_python = unittest.skipUnless(PYTHON_AVAILABLE, "%s 在本机不可执行，跳过需要合法配置的用例" % PYTHON_PATH)
requires_symlink = unittest.skipUnless(hasattr(os, "symlink"), "本机不支持 os.symlink")
# validate() 助手专用哨兵：区分“调用方没传 sha”与“显式传 sha=None（非法值，必须被拒）”。
_OMITTED = object()


def _fingerprint(paths):
    """对显式给出的路径列表做快照（不做目录枚举/扫描）。"""
    snapshot = {}
    for raw in paths:
        path = str(raw)
        try:
            info = os.lstat(path)
        except OSError:
            snapshot[path] = ("absent", None, None)
            continue
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISREG(info.st_mode):
            snapshot[path] = ("file", hashlib.sha256(Path(path).read_bytes()).hexdigest(), mode)
        elif stat.S_ISDIR(info.st_mode):
            snapshot[path] = ("dir", None, mode)
        else:
            snapshot[path] = ("other", None, mode)
    return snapshot


class _FixtureCase(unittest.TestCase):
    """为每个用例建立一个自清理的受控 root（位于 tests/test-artifacts 下）。"""

    def setUp(self):
        _FIXTURES.mkdir(parents=True, exist_ok=True)
        self._temp = tempfile.TemporaryDirectory(prefix="d4cfg-", dir=str(_FIXTURES))
        self.addCleanup(self._temp.cleanup)
        self.root = Path(self._temp.name)
        self.scripts = self.root / "scripts"
        self.scripts.mkdir()
        self.board = self.scripts / "d4_board.py"
        self.board.write_bytes(BOARD_FIXTURE_BYTES)
        self.runtime = self.root / "runtime"
        self.logs = self.runtime / "logs"
        self.logs.mkdir(parents=True)
        self.db = self.root / "approved-d4.sqlite"
        self.db.write_bytes(DB_FIXTURE_BYTES)
        self.catalog = self.root / "catalog.json"
        self.catalog.write_bytes(CATALOG_FIXTURE_BYTES)
        self.board_sha = hashlib.sha256(BOARD_FIXTURE_BYTES).hexdigest()

    def config(self, **overrides):
        base = {
            "schemaVersion": "d4-board-app-config.v1",
            "host": "127.0.0.1",
            "port": 8765,
            "dbPath": str(self.db),
            "catalogPath": None,
            "pythonPath": PYTHON_PATH,
            "boardScript": str(self.board),
            "runtimeDir": str(self.runtime),
            "launchAgentLabel": "com.conductor.d4.board",
        }
        base.update(overrides)
        return base

    def validate(self, config=None, root=None, sha=_OMITTED):
        return appcfg.validate_config(
            self.config() if config is None else config,
            allowed_root=str(self.root) if root is None else root,
            expected_board_sha256=self.board_sha if sha is _OMITTED else sha,
        )


class ValidationTests(_FixtureCase):
    @requires_python
    def test_valid_db_only(self):
        cfg = self.validate()
        self.assertIsInstance(cfg, appcfg.AppConfig)
        self.assertEqual(cfg.schema_version, "d4-board-app-config.v1")
        self.assertEqual(cfg.host, "127.0.0.1")
        self.assertEqual(cfg.port, 8765)
        self.assertEqual(cfg.db_path, str(self.db))
        self.assertIsNone(cfg.catalog_path)
        self.assertEqual(cfg.python_path, PYTHON_PATH)
        self.assertEqual(cfg.board_script, str(self.board))
        self.assertEqual(cfg.runtime_dir, str(self.runtime))
        self.assertEqual(cfg.label, "com.conductor.d4.board")
        self.assertEqual(
            appcfg.build_board_argv(cfg),
            (PYTHON_PATH, "-B", str(self.board), "--db", str(self.db),
             "--runtime-dir", str(self.runtime), "--port", "8765"),
        )

    @requires_python
    def test_valid_catalog_only(self):
        cfg = self.validate(self.config(dbPath=None, catalogPath=str(self.catalog)))
        self.assertIsNone(cfg.db_path)
        self.assertEqual(cfg.catalog_path, str(self.catalog))
        self.assertEqual(
            appcfg.build_board_argv(cfg),
            (PYTHON_PATH, "-B", str(self.board), "--catalog", str(self.catalog),
             "--runtime-dir", str(self.runtime), "--port", "8765"),
        )

    @requires_python
    def test_valid_db_and_catalog_keeps_fixed_order(self):
        cfg = self.validate(self.config(catalogPath=str(self.catalog)))
        self.assertEqual(
            appcfg.build_board_argv(cfg),
            (PYTHON_PATH, "-B", str(self.board), "--db", str(self.db),
             "--catalog", str(self.catalog),
             "--runtime-dir", str(self.runtime), "--port", "8765"),
        )

    @requires_python
    def test_input_mapping_is_not_mutated(self):
        config = self.config()
        before = copy.deepcopy(config)
        self.validate(config)
        self.assertEqual(config, before)

    @requires_python
    def test_app_config_is_immutable_and_scalar_only(self):
        cfg = self.validate()
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.port = 1234
        with self.assertRaises(dataclasses.FrozenInstanceError):
            cfg.db_path = "/tmp/other.sqlite"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            del cfg.db_path
        for value in dataclasses.astuple(cfg):
            self.assertNotIsInstance(value, (dict, list, set))

    @requires_python
    def test_space_and_chinese_paths_are_preserved_verbatim(self):
        zone = self.root / "运行 空间"
        scripts = zone / "子 目录"
        scripts.mkdir(parents=True)
        board = scripts / "d4_board.py"
        board.write_bytes(BOARD_FIXTURE_BYTES)
        runtime = zone / "运行 时"
        (runtime / "logs").mkdir(parents=True)
        db = zone / "数据 库.sqlite"
        db.write_bytes(DB_FIXTURE_BYTES)
        cfg = self.validate(self.config(dbPath=str(db), boardScript=str(board), runtimeDir=str(runtime)))
        self.assertEqual(
            appcfg.build_board_argv(cfg),
            (PYTHON_PATH, "-B", str(board), "--db", str(db),
             "--runtime-dir", str(runtime), "--port", "8765"),
        )
        self.assertIn(str(db), appcfg.build_board_argv(cfg))
        payload = plistlib.loads(appcfg.build_launchd_plist(cfg))
        self.assertEqual(payload["WorkingDirectory"], str(runtime))

    @requires_python
    def test_port_boundaries_accepted(self):
        for port in (1024, 65535, 8765):
            with self.subTest(port=port):
                cfg = self.validate(self.config(port=port))
                self.assertEqual(cfg.port, port)
                self.assertEqual(appcfg.build_board_argv(cfg)[-1], str(port))

    def test_missing_field_rejected(self):
        for name in appcfg.CONFIG_FIELDS:
            with self.subTest(field=name):
                config = self.config()
                config.pop(name)
                with self.assertRaises(ValueError):
                    self.validate(config)

    def test_extra_field_rejected(self):
        for extra in ({"logDir": "/tmp/logs"}, {"argv": []}, {"env": {}}, {"userName": "root"}, {"Port": 8765}):
            with self.subTest(extra=extra):
                config = self.config()
                config.update(extra)
                with self.assertRaises(ValueError):
                    self.validate(config)

    def test_non_mapping_config_rejected(self):
        for bad in ("d4-board-app-config.v1", None, [], 42, b"{}"):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(ValueError):
                    appcfg.validate_config(
                        bad, allowed_root=str(self.root), expected_board_sha256=self.board_sha)

    def test_wrong_schema_version_rejected(self):
        for schema in ("d4-board-app-config.v2", "", "D4-BOARD-APP-CONFIG.V1"):
            with self.subTest(schema=schema):
                with self.assertRaises(ValueError):
                    self.validate(self.config(schemaVersion=schema))

    def test_wrong_host_rejected(self):
        for host in ("0.0.0.0", "localhost", "::1", "127.0.0.1 ", "127.0.0.2", ""):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    self.validate(self.config(host=host))

    def test_port_violations_rejected(self):
        for port in (0, 1, 1023, 65536, 70000, -1, True, False, 8765.0, "8765", None):
            with self.subTest(port=repr(port)):
                with self.assertRaises(ValueError):
                    self.validate(self.config(port=port))

    def test_relative_dotdot_and_noncanonical_paths_rejected(self):
        bad_db = ("relative.sqlite", "./x.sqlite", "../db.sqlite", "~/db.sqlite",
                  str(self.db) + "/", str(self.root) + "//db.sqlite", "/a/../b/db.sqlite")
        for value in bad_db:
            with self.subTest(dbPath=value):
                with self.assertRaises(ValueError):
                    self.validate(self.config(dbPath=value))
        for value in ("scripts/d4_board.py", "../scripts/d4_board.py"):
            with self.subTest(boardScript=value):
                with self.assertRaises(ValueError):
                    self.validate(self.config(boardScript=value))
        for value in ("runtime", "../runtime", "runtime/"):
            with self.subTest(runtimeDir=value):
                with self.assertRaises(ValueError):
                    self.validate(self.config(runtimeDir=value))

    def test_control_and_illegal_unicode_rejected(self):
        suffixes = ("\x00", "\n", "\t", "\x1b", "\x7f", "\u202e", "\ud800", "\u009f")
        for suffix in suffixes:
            with self.subTest(suffix=repr(suffix)):
                with self.assertRaises(ValueError):
                    self.validate(self.config(dbPath=str(self.db) + suffix))
        with self.assertRaises(ValueError):
            self.validate(self.config(catalogPath=""))
        with self.assertRaises(ValueError):
            self.validate(self.config(runtimeDir=str(self.runtime) + "\x00"))

    def test_out_of_root_paths_rejected(self):
        with tempfile.TemporaryDirectory(prefix="d4cfg-out-", dir=str(_FIXTURES)) as other:
            outside_db = Path(other) / "outside.sqlite"
            outside_db.write_bytes(DB_FIXTURE_BYTES)
            outside_scripts = Path(other) / "scripts"
            outside_scripts.mkdir()
            outside_board = outside_scripts / "d4_board.py"
            outside_board.write_bytes(BOARD_FIXTURE_BYTES)
            with self.subTest(field="dbPath"):
                with self.assertRaises(ValueError):
                    self.validate(self.config(dbPath=str(outside_db)))
            with self.subTest(field="catalogPath"):
                with self.assertRaises(ValueError):
                    self.validate(self.config(dbPath=None, catalogPath=str(outside_db)))
            with self.subTest(field="boardScript"):
                with self.assertRaises(ValueError):
                    self.validate(self.config(boardScript=str(outside_board)))
            with self.subTest(field="runtimeDir"):
                with self.assertRaises(ValueError):
                    self.validate(self.config(runtimeDir=str(Path(other))))

    def test_temporary_paths_rejected_for_every_path_field(self):
        for prefix in ("/tmp", "/private/tmp", "/var/folders/aa/bb", "/private/var/folders/aa/bb"):
            with self.subTest(prefix=prefix):
                with self.assertRaises(ValueError):
                    self.validate(self.config(dbPath=prefix + "/d4.sqlite"))
                with self.assertRaises(ValueError):
                    self.validate(self.config(dbPath=None, catalogPath=prefix + "/catalog.json"))
                with self.assertRaises(ValueError):
                    self.validate(self.config(boardScript=prefix + "/scripts/d4_board.py"))
                with self.assertRaises(ValueError):
                    self.validate(self.config(runtimeDir=prefix + "/runtime"))
                with self.assertRaises(ValueError):
                    self.validate(self.config(), root=prefix)

    def test_allowed_root_violations_rejected(self):
        with self.subTest(case="missing"):
            with self.assertRaises(ValueError):
                self.validate(self.config(), root=str(self.root / "nope"))
        with self.subTest(case="is_file"):
            with self.assertRaises(ValueError):
                self.validate(self.config(), root=str(self.db))
        with self.subTest(case="relative"):
            with self.assertRaises(ValueError):
                self.validate(self.config(), root="relative/root")
        with self.subTest(case="temporary"):
            with self.assertRaises(ValueError):
                self.validate(self.config(), root="/tmp")

    def test_db_path_must_be_regular_file(self):
        with self.subTest(case="directory"):
            with self.assertRaises(ValueError):
                self.validate(self.config(dbPath=str(self.scripts)))
        with self.subTest(case="missing"):
            with self.assertRaises(ValueError):
                self.validate(self.config(dbPath=str(self.root / "missing.sqlite")))

    def test_catalog_path_must_be_regular_file(self):
        with self.assertRaises(ValueError):
            self.validate(self.config(dbPath=None, catalogPath=str(self.scripts)))

    def test_missing_db_and_catalog_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(self.config(dbPath=None, catalogPath=None))

    def test_empty_string_paths_rejected(self):
        with self.assertRaises(ValueError):
            self.validate(self.config(dbPath=""))
        with self.assertRaises(ValueError):
            self.validate(self.config(dbPath=None, catalogPath=""))

    def test_runtime_dir_violations_rejected(self):
        with self.subTest(case="is_file"):
            with self.assertRaises(ValueError):
                self.validate(self.config(runtimeDir=str(self.db)))
        with self.subTest(case="missing"):
            with self.assertRaises(ValueError):
                self.validate(self.config(runtimeDir=str(self.root / "missing-runtime")))
        with self.subTest(case="logs_missing"):
            bare = self.root / "bare-runtime"
            bare.mkdir()
            with self.assertRaises(ValueError):
                self.validate(self.config(runtimeDir=str(bare)))
        with self.subTest(case="logs_is_file"):
            flat = self.root / "flat-runtime"
            flat.mkdir()
            (flat / "logs").write_bytes(b"not-a-directory")
            with self.assertRaises(ValueError):
                self.validate(self.config(runtimeDir=str(flat)))

    def test_board_script_name_and_sha_rules(self):
        with self.subTest(case="basename"):
            other = self.scripts / "d4_collect.py"
            other.write_bytes(BOARD_FIXTURE_BYTES)
            with self.assertRaises(ValueError):
                self.validate(self.config(boardScript=str(other)))
        with self.subTest(case="missing"):
            with self.assertRaises(ValueError):
                self.validate(self.config(boardScript=str(self.root / "nope-scripts" / "d4_board.py")))
        with self.subTest(case="wrong_sha"):
            with self.assertRaises(ValueError):
                self.validate(sha="0" * 64)
        with self.subTest(case="uppercase_sha"):
            with self.assertRaises(ValueError):
                self.validate(sha=self.board_sha.upper())
        with self.subTest(case="short_sha"):
            with self.assertRaises(ValueError):
                self.validate(sha=self.board_sha[:63])
        with self.subTest(case="long_sha"):
            with self.assertRaises(ValueError):
                self.validate(sha=self.board_sha + "0")
        with self.subTest(case="non_hex_sha"):
            with self.assertRaises(ValueError):
                self.validate(sha="g" * 64)
        with self.subTest(case="sha_wrong_type"):
            with self.assertRaises(ValueError):
                self.validate(sha=None)
        with self.subTest(case="modified_board_source"):
            self.board.write_bytes(BOARD_FIXTURE_BYTES + b"# changed\n")
            with self.assertRaises(ValueError):
                self.validate()
            self.board.write_bytes(BOARD_FIXTURE_BYTES)

    def test_fake_python_paths_rejected(self):
        for value in ("/usr/bin/python", "/bin/sh", "python3", "/usr/bin/python3.9", "", "/usr/local/bin/python3"):
            with self.subTest(pythonPath=value):
                with self.assertRaises(ValueError):
                    self.validate(self.config(pythonPath=value))

    def test_injected_label_rejected(self):
        for label in ("com.evil.d4.board", "com.conductor.d4.board ", "", "com.conductor.d4.BOARD"):
            with self.subTest(label=label):
                with self.assertRaises(ValueError):
                    self.validate(self.config(launchAgentLabel=label))

    @requires_python
    def test_python_path_environment_precondition(self):
        info = os.stat(PYTHON_PATH)
        self.assertTrue(stat.S_ISREG(info.st_mode), "%s 必须是普通文件" % PYTHON_PATH)
        self.assertTrue(os.access(PYTHON_PATH, os.X_OK), "%s 必须可执行" % PYTHON_PATH)


class SymlinkTests(_FixtureCase):
    @requires_symlink
    def test_db_symlink_rejected(self):
        real = self.root / "real.sqlite"
        real.write_bytes(DB_FIXTURE_BYTES)
        link = self.root / "linked.sqlite"
        os.symlink(str(real), str(link))
        with self.assertRaises(ValueError):
            self.validate(self.config(dbPath=str(link)))

    @requires_symlink
    def test_board_script_symlink_rejected(self):
        link = self.root / "d4_board.py"
        os.symlink(str(self.board), str(link))
        with self.assertRaises(ValueError):
            self.validate(self.config(boardScript=str(link)))

    @requires_symlink
    def test_intermediate_symlinked_directory_rejected(self):
        alias = self.root / "alias"
        os.symlink(str(self.scripts), str(alias))
        with self.assertRaises(ValueError):
            self.validate(self.config(boardScript=str(alias / "d4_board.py")))

    @requires_symlink
    def test_symlinked_runtime_dir_rejected(self):
        real_runtime = self.root / "real-runtime"
        (real_runtime / "logs").mkdir(parents=True)
        alias_runtime = self.root / "alias-runtime"
        os.symlink(str(real_runtime), str(alias_runtime))
        with self.assertRaises(ValueError):
            self.validate(self.config(runtimeDir=str(alias_runtime)))

    @requires_symlink
    def test_logs_directory_symlink_rejected(self):
        broken = self.root / "broken-runtime"
        broken.mkdir()
        os.symlink(str(self.root / "absent-logs"), str(broken / "logs"))
        with self.assertRaises(ValueError):
            self.validate(self.config(runtimeDir=str(broken)))

    @requires_symlink
    def test_allowed_root_symlink_rejected(self):
        alias_root = Path(str(self.root) + "-alias")
        os.symlink(str(self.root), str(alias_root))
        try:
            with self.assertRaises(ValueError):
                self.validate(self.config(), root=str(alias_root))
        finally:
            alias_root.unlink()

    @requires_symlink
    def test_allowed_root_ancestor_symlink_rejected(self):
        """allowed_root 自身不是 symlink，但其祖先某一层是 symlink 时也必须拒绝（F1 回归）。

        修好前：lexical root 的末层是真目录，旧实现只查末层，于是“经 symlink 绕入的
        受控 root”会被静默接受，本用例在旧实现下必失败；修好后抛 ValueError。
        fixture 仍建在 self.root（tests/test-artifacts 下），不使用系统默认临时目录。
        """
        real = self.root / "real"
        nested = real / "nested"
        (nested / "scripts").mkdir(parents=True)
        (nested / "logs-holder").mkdir()
        (nested / "runtime" / "logs").mkdir(parents=True)
        (nested / "scripts" / "d4_board.py").write_bytes(BOARD_FIXTURE_BYTES)
        (nested / "nested.sqlite").write_bytes(DB_FIXTURE_BYTES)
        alias = self.root / "alias"
        os.symlink(str(real), str(alias))
        lexical_root = alias / "nested"
        self.assertFalse(os.path.islink(str(lexical_root)), "末层必须是真目录，缺陷只在祖先层")
        with self.assertRaises(ValueError):
            appcfg.validate_config(
                self.config(
                    dbPath=str(lexical_root / "nested.sqlite"),
                    boardScript=str(lexical_root / "scripts" / "d4_board.py"),
                    runtimeDir=str(lexical_root / "runtime"),
                ),
                allowed_root=str(lexical_root),
                expected_board_sha256=self.board_sha,
            )

    @requires_symlink
    def test_existing_log_target_symlink_rejected(self):
        os.symlink(str(self.root / "absent.log"), str(self.logs / "board.out.log"))
        with self.assertRaises(ValueError):
            self.validate()

    @requires_symlink
    def test_log_target_becoming_symlink_after_validation_rejected(self):
        cfg = self.validate()
        os.symlink(str(self.root / "absent.log"), str(self.logs / "board.err.log"))
        with self.assertRaises(ValueError):
            appcfg.build_launchd_plist(cfg)

    def test_existing_log_target_regular_file_accepted(self):
        (self.logs / "board.out.log").write_bytes(b"previous output\n")
        (self.logs / "board.err.log").write_bytes(b"previous error\n")
        cfg = self.validate()
        payload = plistlib.loads(appcfg.build_launchd_plist(cfg))
        self.assertEqual(payload["StandardOutPath"], str(self.logs / "board.out.log"))
        self.assertEqual(payload["StandardErrorPath"], str(self.logs / "board.err.log"))


class BuilderTests(_FixtureCase):
    @requires_python
    def test_argv_is_tuple_of_single_elements(self):
        cfg = self.validate(self.config(catalogPath=str(self.catalog)))
        argv = appcfg.build_board_argv(cfg)
        self.assertIsInstance(argv, tuple)
        for element in argv:
            self.assertIsInstance(element, str)
        self.assertEqual(argv[0], PYTHON_PATH)
        self.assertEqual(argv[1], "-B")
        self.assertEqual(argv[2], str(self.board))
        self.assertEqual(argv[-4], "--runtime-dir")
        self.assertEqual(argv[-3], str(self.runtime))
        self.assertEqual(argv[-2], "--port")
        self.assertEqual(argv[-1], "8765")
        self.assertNotIn(" ", argv[2])

    @requires_python
    def test_plist_payload_is_exact(self):
        cfg = self.validate()
        raw = appcfg.build_launchd_plist(cfg)
        self.assertIsInstance(raw, bytes)
        payload = plistlib.loads(raw)
        self.assertEqual(payload, {
            "Label": "com.conductor.d4.board",
            "ProgramArguments": [PYTHON_PATH, "-B", str(self.board), "--db", str(self.db),
                                 "--runtime-dir", str(self.runtime), "--port", "8765"],
            "WorkingDirectory": str(self.runtime),
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 10,
            "StandardOutPath": str(self.logs / "board.out.log"),
            "StandardErrorPath": str(self.logs / "board.err.log"),
            "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
            "ProcessType": "Background",
        })
        for forbidden in ("Sockets", "MachServices", "UserName", "GroupName", "InitGroups",
                          "Collect", "WatchPaths", "KeepAliveSuccessfulExit"):
            self.assertNotIn(forbidden, payload)
        self.assertNotIn("launchctl", raw.decode("utf-8"))

    @requires_python
    def test_plist_bytes_are_deterministic(self):
        cfg = self.validate()
        self.assertEqual(appcfg.build_launchd_plist(cfg), appcfg.build_launchd_plist(cfg))

    @requires_python
    def test_plist_roundtrip_preserves_types(self):
        cfg = self.validate(self.config(dbPath=None, catalogPath=str(self.catalog)))
        payload = plistlib.loads(appcfg.build_launchd_plist(cfg))
        self.assertIs(payload["RunAtLoad"], True)
        self.assertIs(payload["KeepAlive"]["SuccessfulExit"], False)
        self.assertEqual(payload["ThrottleInterval"], 10)
        self.assertEqual(payload["ProgramArguments"], list(appcfg.build_board_argv(cfg)))

    @requires_python
    def test_builders_do_not_write_any_file(self):
        cfg = self.validate()
        watched = [self.db, self.catalog, self.board, self.runtime, self.logs,
                   self.logs / "board.out.log", self.logs / "board.err.log"]
        before = _fingerprint(watched)
        appcfg.build_board_argv(cfg)
        appcfg.build_launchd_plist(cfg)
        self.assertEqual(_fingerprint(watched), before)

    def test_builders_reject_non_app_config(self):
        for bad in ({"python_path": PYTHON_PATH}, None, "argv", 7):
            with self.subTest(kind=type(bad).__name__):
                with self.assertRaises(TypeError):
                    appcfg.build_board_argv(bad)
                with self.assertRaises(TypeError):
                    appcfg.build_launchd_plist(bad)


class ZeroSideEffectTests(_FixtureCase):
    def test_error_paths_create_no_file_and_change_nothing(self):
        missing_db = self.root / "missing.sqlite"
        watched = [self.db, self.catalog, self.board, self.runtime, self.logs, missing_db,
                   Path(str(missing_db) + "-journal"), Path(str(missing_db) + "-wal")]
        before = _fingerprint(watched)
        cases = [
            ("missing_db", self.config(dbPath=str(missing_db)), {}),
            ("relative_db", self.config(dbPath="relative.sqlite"), {}),
            ("tmp_db", self.config(dbPath="/tmp/d4-evil.sqlite"), {}),
            ("db_is_dir", self.config(dbPath=str(self.scripts)), {}),
            ("port_zero", self.config(port=0), {}),
            ("bad_host", self.config(host="0.0.0.0"), {}),
            ("bad_schema", self.config(schemaVersion="nope"), {}),
            ("bad_label", self.config(launchAgentLabel="com.evil.board"), {}),
            ("fake_python", self.config(pythonPath="/usr/bin/python"), {}),
            ("extra_field", dict(self.config(), logDir="/tmp/logs"), {}),
            ("missing_field", {k: v for k, v in self.config().items() if k != "runtimeDir"}, {}),
            ("wrong_sha", self.config(), {"sha": "0" * 64}),
            ("out_of_root", self.config(boardScript="/usr/bin/python3"), {}),
            ("no_db_no_catalog", self.config(dbPath=None, catalogPath=None), {}),
        ]
        for name, config, kwargs in cases:
            with self.subTest(case=name):
                with self.assertRaises(ValueError):
                    self.validate(config, **kwargs)
        self.assertEqual(_fingerprint(watched), before)
        self.assertFalse(missing_db.exists())

    @requires_python
    def test_successful_validation_does_not_modify_inputs(self):
        watched = [self.db, self.catalog, self.board]
        before = _fingerprint(watched)
        cfg = self.validate(self.config(catalogPath=str(self.catalog)))
        appcfg.build_board_argv(cfg)
        appcfg.build_launchd_plist(cfg)
        self.assertEqual(_fingerprint(watched), before)
        self.assertEqual(self.db.read_bytes(), DB_FIXTURE_BYTES)


class StaticSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.source = (_SCRIPTS / "d4_app_config.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_forbidden_imports(self):
        forbidden = {"socket", "subprocess", "http", "urllib", "sqlite3", "ssl", "ftplib",
                     "shutil", "argparse", "multiprocessing", "ctypes", "asyncio", "requests",
                     "webbrowser", "launchctl"}
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & forbidden, set())

    def test_no_command_line_entry_point(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.If):
                names = {child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)}
                self.assertNotIn("__name__", names, "候选模块不得包含 __main__ 入口")

    def test_top_level_statements_have_no_side_effects(self):
        allowed = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.ClassDef, ast.Assign, ast.AnnAssign, ast.Expr)
        for node in self.tree.body:
            self.assertIsInstance(node, allowed, "顶层出现意外语句：%s" % type(node).__name__)
            if isinstance(node, ast.Expr):
                self.assertIsInstance(node.value, ast.Constant, "顶层只允许文档字符串表达式")

    def test_no_process_shell_or_write_calls(self):
        banned_attrs = {"system", "popen", "fork", "kill", "remove", "unlink", "rmdir", "mkdir",
                        "makedirs", "rename", "replace", "chmod", "chown", "truncate", "write",
                        "spawn", "spawnl", "spawnv"}
        banned_names = {"open", "eval", "exec", "compile", "__import__", "input"}
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os":
                self.assertFalse(func.attr.startswith("exec"), "禁止 os.%s" % func.attr)
                self.assertFalse(func.attr.startswith("spawn"), "禁止 os.%s" % func.attr)
                self.assertNotIn(func.attr, banned_attrs, "禁止 os.%s" % func.attr)
            if isinstance(func, ast.Name):
                self.assertNotIn(func.id, banned_names, "禁止内置 %s" % func.id)

    def test_module_namespace_has_no_service_surface(self):
        for name in ("socket", "subprocess", "http", "urllib", "sqlite3", "launchctl",
                     "main", "cli", "run", "serve", "start", "install", "load", "bootstrap"):
            self.assertFalse(hasattr(appcfg, name), "模块不应暴露 %s" % name)
        self.assertNotIn("launchctl", self.source)

    def test_public_api_is_exactly_the_contract(self):
        self.assertTrue(callable(appcfg.validate_config))
        self.assertTrue(callable(appcfg.build_board_argv))
        self.assertTrue(callable(appcfg.build_launchd_plist))
        self.assertTrue(dataclasses.is_dataclass(appcfg.AppConfig))
        self.assertEqual(sorted(appcfg.__all__),
                         ["AppConfig", "build_board_argv", "build_launchd_plist", "validate_config"])

    def test_config_fields_are_exactly_nine(self):
        expected = ["boardScript", "catalogPath", "dbPath", "host", "launchAgentLabel",
                    "port", "pythonPath", "runtimeDir", "schemaVersion"]
        self.assertEqual(len(appcfg.CONFIG_FIELDS), 9)
        self.assertEqual(sorted(appcfg.CONFIG_FIELDS), expected)
        self.assertEqual(sorted(field.name for field in dataclasses.fields(appcfg.AppConfig)),
                         ["board_script", "catalog_path", "db_path", "host", "label",
                          "port", "python_path", "runtime_dir", "schema_version"])


class SideEffectTrapTests(_FixtureCase):
    """网络/进程 trap 仅作负面证据，不构成硬隔离证明。"""

    @requires_python
    def test_no_network_or_process_call_during_validation_and_builders(self):
        with mock.patch("socket.socket", side_effect=AssertionError("network trap")) as net, \
                mock.patch("subprocess.Popen", side_effect=AssertionError("process trap")) as proc, \
                mock.patch("subprocess.run", side_effect=AssertionError("process trap")) as proc_run, \
                mock.patch("os.system", side_effect=AssertionError("shell trap")) as shell:
            cfg = self.validate(self.config(catalogPath=str(self.catalog)))
            appcfg.build_board_argv(cfg)
            appcfg.build_launchd_plist(cfg)
            with self.assertRaises(ValueError):
                self.validate(self.config(port=0))
            with self.assertRaises(TypeError):
                appcfg.build_board_argv({"python_path": PYTHON_PATH})
        self.assertFalse(net.called)
        self.assertFalse(proc.called)
        self.assertFalse(proc_run.called)
        self.assertFalse(shell.called)


if __name__ == "__main__":
    unittest.main()
