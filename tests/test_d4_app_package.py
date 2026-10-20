"""candidate/scripts/d4_app_package.py 的离线 unittest 套件（G2-D 打包候选）。

NOT_RUN in this attempt
-----------------------
本 worker ``Bash=[]``：本轮没有执行任何命令、没有运行任何用例、没有重算任何 SHA。下面每个
用例都是 **NOT_RUN**，不是 PASS；真实运行、范围与冻结字节的独立核验属于 cox。

覆盖范围
--------
* 包布局（``D4.app`` 内部结构 + 同级 ``config.json`` / launchd plist / ``D4.command``）；
* Info.plist 的可执行文件 / 图标 / 标识 / 名称 / 包类型；
* launchd plist 与 G1 生成器字节一致（固定 argv / runtimeDir / logs / KeepAlive / Throttle）；
* ICNS 头与长度、PNG 表示、以及 “D4” 标签字形；
* 启动 argv 在含空格与引号的路径下的 ``shlex.quote`` 转义；
* config.json 的规范字节与 sha256 固定（供 ``d4_app_entry`` 校验）；
* 权限位（launcher 0755、config/plist 0600、icon 0644）；
* 不覆盖已有输出、不越出 ``artifact_root``、拒绝符号链接与安装/临时位置；
* 来源不可用时拒发且不产生任何输出；
* 成功构建不改动任何输入文件，失败构建回滚且不影响同级邻居；
* 构建过程无进程、无网络；合成“服务重启”通过重新加载已接受模块/身份 + 只读读取自有
  fixture 数据库证明数据未变（真实 macOS 行为 NOT_RUN）。

fixture 与安全
--------------
每个 fixture 位于 ``candidate/tests/test-artifacts`` 下的一个 ``tempfile.mkdtemp`` 目录内，
用例结束即清理；冻结 G1 校验拒绝 ``/tmp`` 等临时路径，所以 fixture 从不放在那里。清单里冻结的
``candidate/tests/fixtures/d4_readiness_schema.sql`` 只被只读执行，绝不修改。
生成的启动脚本与 ``/bin/sh`` / 启动服务在本套件中**从不执行**：只做文本解析与断言。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import plistlib
import shlex
import shutil
import sqlite3
import stat
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

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


pkg = _load("d4_app_package", "d4_app_package.py")
# 复用 d4_app_package 自己加载的那份模块对象，避免 AppConfig 类出现两个副本。
appcfg = pkg._APP_CONFIG
rt = pkg._HEALTH_RUNTIME
entry = _load("d4_app_entry", "d4_app_entry.py")

PYTHON_PATH = "/usr/bin/python3"
PYTHON_AVAILABLE = os.path.isfile(PYTHON_PATH) and os.access(PYTHON_PATH, os.X_OK)
requires_python = unittest.skipUnless(
    PYTHON_AVAILABLE, "%s is not an executable file on this host" % PYTHON_PATH
)
requires_symlink = unittest.skipUnless(hasattr(os, "symlink"), "os.symlink is not available")

BOARD_SOURCE = '"""D4 board entry test double (fixture only)."""\nVALUE = 1\n'
OUTPUT_NAME = "D4 package"


def _fingerprint(paths):
    """对显式给出的路径列表做快照（不做目录枚举）。"""
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


def _parse_png(raw):
    """解析 PNG 块序列并校验每块 CRC。"""
    if raw[:8] != b"\x89PNG\r\n\x1a\n":
        raise AssertionError("bad PNG signature")
    offset = 8
    chunks = []
    while offset < len(raw):
        length = struct.unpack(">I", raw[offset:offset + 4])[0]
        tag = raw[offset + 4:offset + 8]
        data = raw[offset + 8:offset + 8 + length]
        crc = struct.unpack(">I", raw[offset + 8 + length:offset + 12 + length])[0]
        if crc != (zlib.crc32(tag + data) & 0xFFFFFFFF):
            raise AssertionError("bad PNG chunk CRC")
        chunks.append((tag, data))
        offset += 12 + length
    return chunks


def _decode_png_rgba(raw):
    """把 PNG 解回 RGBA 行（本编码器只用 filter 0）。"""
    chunks = _parse_png(raw)
    tags = [tag for tag, _data in chunks]
    if tags[0] != b"IHDR" or tags[-1] != b"IEND":
        raise AssertionError("unexpected PNG chunk order")
    header = dict(chunks)[b"IHDR"]
    width, height, depth, color, compression, filtering, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0):
        raise AssertionError("unexpected PNG header fields")
    idat = b"".join(data for tag, data in chunks if tag == b"IDAT")
    scanlines = zlib.decompress(idat)
    if len(scanlines) != height * (1 + width * 4):
        raise AssertionError("unexpected scanline byte count")
    stride = width * 4
    rows = []
    offset = 0
    for _ in range(height):
        filter_type = scanlines[offset]
        offset += 1
        if filter_type != 0:
            raise AssertionError("unexpected scanline filter")
        rows.append(bytearray(scanlines[offset:offset + stride]))
        offset += stride
    return width, height, rows


def _icns_payload(raw):
    """拆出 ICNS 头与唯一 PNG 表示块，返回 (declared_length, chunk_type, chunk_length, png)。"""
    if raw[:4] != b"icns":
        raise AssertionError("bad ICNS magic")
    declared = struct.unpack(">I", raw[4:8])[0]
    chunk_type = raw[8:12]
    chunk_length = struct.unpack(">I", raw[12:16])[0]
    return declared, chunk_type, chunk_length, raw[16:]


class PackageFixtureTestCase(unittest.TestCase):
    """每个用例建立一个自清理的受控 root（位于 tests/test-artifacts 下）。"""

    def setUp(self):
        _TEST_ARTIFACTS.mkdir(parents=True, exist_ok=True)
        self._sandbox = Path(
            tempfile.mkdtemp(prefix="d4-app-package-", dir=str(_TEST_ARTIFACTS))
        )
        self.addCleanup(shutil.rmtree, self._sandbox, ignore_errors=True)

        # 路径里刻意带上空格与单引号，用来检验启动 argv 的转义。
        self.root = self._sandbox / "工作 目录'space"
        self.root.mkdir()
        self.app_dir = self.root / "app scripts"
        self.app_dir.mkdir()
        self.board_script = self.app_dir / "d4_board.py"
        self.board_script.write_text(BOARD_SOURCE, encoding="utf-8")
        self.board_sha = hashlib.sha256(BOARD_SOURCE.encode("utf-8")).hexdigest()

        self.db_path = self.root / "board 数据.sqlite3"
        self.catalog_path = self.root / "catalog json.json"
        self.runtime_dir = self.root / "runtime dir"
        self.runtime_dir.mkdir()
        (self.runtime_dir / "logs").mkdir()
        self.artifact_root = self.root / "artifact root"
        self.artifact_root.mkdir()
        self.prepare_ready()

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

    def write_catalog(self):
        document = {
            "schemaVersion": "project-catalog.v1",
            "projects": [
                {
                    "projectId": "d4-product",
                    "name": "D4 看板",
                    "milestones": [{"id": "m-1", "name": "M1"}],
                    "tasks": [
                        {
                            "taskId": "task-catalog-1",
                            "milestoneId": "m-1",
                            "status": "verified",
                            "documents": [],
                        }
                    ],
                    "issues": [],
                }
            ],
        }
        self.catalog_path.write_text(
            json.dumps(document, ensure_ascii=False, sort_keys=True), encoding="utf-8"
        )

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

    def prepare_ready(self):
        """有效、无项目冲突的 fixture：库内 project id 与 catalog 的 id 不同。"""
        self.write_database(projects=("d4-product-live",))
        self.write_catalog()
        return self.config()

    # -- build helper --------------------------------------------------------
    def build(self, *, config=None, output_name=OUTPUT_NAME, artifact_root=None, **kwargs):
        root = self.artifact_root if artifact_root is None else artifact_root
        return pkg.build_package(
            self.config() if config is None else config,
            allowed_root=str(self.root),
            expected_board_sha256=self.board_sha,
            artifact_root=str(root),
            output_name=output_name,
            **kwargs,
        )

    def launcher_argv(self, output_root, relative="D4.app/Contents/MacOS/D4"):
        text = (Path(output_root) / relative).read_bytes().decode("utf-8")
        lines = text.splitlines()
        exec_lines = [line for line in lines if line.startswith("exec ")]
        self.assertEqual(len(exec_lines), 1, "启动脚本必须只有一条 exec 行")
        return lines, exec_lines[0], shlex.split(exec_lines[0])[1:]


# --------------------------------------------------------------------------- #
# 包布局与 manifest
# --------------------------------------------------------------------------- #
@requires_python
class LayoutTests(PackageFixtureTestCase):
    EXPECTED = [
        "D4.app/Contents/Info.plist",
        "D4.app/Contents/MacOS/D4",
        "D4.app/Contents/Resources/D4.icns",
        "D4.command",
        "com.conductor.d4.board.plist",
        "config.json",
    ]

    def test_expected_layout_is_created(self):
        manifest = self.build()
        out = Path(manifest["outputRoot"])
        self.assertEqual(out, self.artifact_root / OUTPUT_NAME)
        self.assertEqual(os.path.dirname(manifest["outputRoot"]), str(self.artifact_root))
        self.assertEqual(sorted(item["path"] for item in manifest["files"]), sorted(self.EXPECTED))
        for relative in self.EXPECTED:
            self.assertTrue((out / relative).is_file(), relative)
        self.assertTrue((out / "D4.app" / "Contents" / "MacOS").is_dir())
        self.assertTrue((out / "D4.app" / "Contents" / "Resources").is_dir())

    def test_output_root_contains_only_expected_top_level_entries(self):
        manifest = self.build()
        self.assertEqual(
            sorted(os.listdir(manifest["outputRoot"])),
            ["D4.app", "D4.command", "com.conductor.d4.board.plist", "config.json"],
        )
        self.assertEqual(
            sorted(os.listdir(os.path.join(manifest["outputRoot"], "D4.app", "Contents"))),
            ["Info.plist", "MacOS", "Resources"],
        )

    def test_manifest_digests_and_modes_match_disk(self):
        manifest = self.build()
        for item in manifest["files"]:
            target = Path(manifest["outputRoot"]) / item["path"]
            raw = target.read_bytes()
            self.assertEqual(item["bytes"], len(raw), item["path"])
            self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest(), item["path"])
            self.assertEqual(
                item["mode"], "0%03o" % stat.S_IMODE(os.lstat(target).st_mode), item["path"]
            )

    def test_manifest_identity_fields(self):
        manifest = self.build()
        self.assertEqual(manifest["schemaVersion"], "d4-app-package-manifest.v1")
        self.assertEqual(manifest["bundleName"], "D4.app")
        self.assertEqual(manifest["launchAgentLabel"], "com.conductor.d4.board")
        self.assertEqual(manifest["outputName"], OUTPUT_NAME)
        self.assertEqual(manifest["boardSha256"], self.board_sha)
        config_raw = Path(manifest["configPath"]).read_bytes()
        self.assertEqual(manifest["configSha256"], hashlib.sha256(config_raw).hexdigest())
        self.assertEqual(manifest["configPath"], manifest["outputRoot"] + "/config.json")

    def test_manifest_entry_identity_points_at_the_accepted_entry(self):
        manifest = self.build()
        expected_entry = str(_SCRIPTS_DIR / "d4_app_entry.py")
        self.assertEqual(manifest["entryScript"], expected_entry)
        self.assertEqual(
            manifest["entrySha256"],
            hashlib.sha256(Path(expected_entry).read_bytes()).hexdigest(),
        )


# --------------------------------------------------------------------------- #
# Info.plist / launchd plist
# --------------------------------------------------------------------------- #
@requires_python
class InfoPlistTests(PackageFixtureTestCase):
    def test_info_plist_identity_keys(self):
        manifest = self.build()
        payload = plistlib.loads(
            (Path(manifest["outputRoot"]) / "D4.app/Contents/Info.plist").read_bytes()
        )
        self.assertEqual(payload["CFBundleExecutable"], "D4")
        self.assertEqual(payload["CFBundleIconFile"], "D4.icns")
        self.assertEqual(payload["CFBundlePackageType"], "APPL")
        self.assertEqual(payload["CFBundleName"], "D4")
        self.assertEqual(payload["CFBundleDisplayName"], "D4")
        self.assertEqual(payload["CFBundleIdentifier"], "com.conductor.d4.board")
        self.assertEqual(
            set(payload),
            {
                "CFBundleName",
                "CFBundleDisplayName",
                "CFBundleIdentifier",
                "CFBundleExecutable",
                "CFBundleIconFile",
                "CFBundlePackageType",
                "CFBundleInfoDictionaryVersion",
            },
        )

    def test_info_plist_bytes_are_deterministic(self):
        self.assertEqual(pkg._info_plist_bytes(), pkg._info_plist_bytes())


@requires_python
class LaunchdPlistTests(PackageFixtureTestCase):
    def test_launchd_plist_is_exactly_the_g1_generator_bytes(self):
        config = self.prepare_ready()
        validated = appcfg.validate_config(
            config, allowed_root=str(self.root), expected_board_sha256=self.board_sha
        )
        manifest = self.build(config=config)
        raw = (Path(manifest["outputRoot"]) / "com.conductor.d4.board.plist").read_bytes()
        self.assertEqual(raw, appcfg.build_launchd_plist(validated))

    def test_launchd_plist_keeps_fixed_argv_and_keepalive(self):
        config = self.prepare_ready()
        validated = appcfg.validate_config(
            config, allowed_root=str(self.root), expected_board_sha256=self.board_sha
        )
        manifest = self.build(config=config)
        payload = plistlib.loads(
            (Path(manifest["outputRoot"]) / "com.conductor.d4.board.plist").read_bytes()
        )
        self.assertEqual(payload["Label"], "com.conductor.d4.board")
        self.assertEqual(payload["ProgramArguments"], list(appcfg.build_board_argv(validated)))
        self.assertEqual(payload["WorkingDirectory"], str(self.runtime_dir))
        self.assertEqual(payload["KeepAlive"], {"SuccessfulExit": False})
        self.assertEqual(payload["ThrottleInterval"], 10)
        self.assertEqual(
            payload["StandardOutPath"], str(self.runtime_dir / "logs" / "board.out.log")
        )
        self.assertEqual(
            payload["StandardErrorPath"], str(self.runtime_dir / "logs" / "board.err.log")
        )


# --------------------------------------------------------------------------- #
# 图标
# --------------------------------------------------------------------------- #
@requires_python
class IconTests(PackageFixtureTestCase):
    def test_icns_header_and_lengths_are_valid(self):
        raw = pkg.build_icns()
        declared, chunk_type, chunk_length, png = _icns_payload(raw)
        self.assertEqual(declared, len(raw))
        self.assertEqual(chunk_type, b"ic07")
        self.assertEqual(chunk_length, len(raw) - 8)
        self.assertEqual(chunk_length, 8 + len(png))
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_icns_png_encodes_the_d4_label(self):
        raw = pkg.build_icns()
        _declared, _chunk_type, _chunk_length, png = _icns_payload(raw)
        width, height, rows = _decode_png_rgba(png)
        self.assertEqual((width, height), (128, 128))

        background = bytes(pkg.ICON_BACKGROUND)
        foreground = bytes(pkg.ICON_FOREGROUND)

        # PNG 忠实编码了渲染结果。
        expected = [bytes(row) for row in pkg._render_label()]
        self.assertEqual([bytes(row) for row in rows], expected)

        # 独立计数：“D”与“4”的字形位数 × 缩放平方。
        glyph_bits = sum(line.count("1") for line in pkg.GLYPHS["D"])
        glyph_bits += sum(line.count("1") for line in pkg.GLYPHS["4"])
        lit = sum(
            1
            for row in rows
            for offset in range(0, len(row), 4)
            if bytes(row[offset:offset + 4]) == foreground
        )
        self.assertEqual(lit, glyph_bits * pkg.ICON_SCALE ** 2)

        # 背景色存在，且“D”“4”是两个分离的簇。
        self.assertEqual(bytes(rows[0][:4]), background)
        self.assertEqual(bytes(rows[-1][-4:]), background)
        columns = sorted(
            {
                offset // 4
                for row in rows
                for offset in range(0, len(row), 4)
                if bytes(row[offset:offset + 4]) == foreground
            }
        )
        gaps = [second - first for first, second in zip(columns, columns[1:]) if second - first > 1]
        self.assertEqual(len(gaps), 1, "图标应当恰好是两个分离字形")
        self.assertGreater(columns[-1] - columns[0], 0)

    def test_icns_is_deterministic(self):
        self.assertEqual(pkg.build_icns(), pkg.build_icns())


# --------------------------------------------------------------------------- #
# 启动脚本与 argv 转义
# --------------------------------------------------------------------------- #
@requires_python
class LaunchScriptTests(PackageFixtureTestCase):
    def test_launcher_argv_is_exact_and_quoted(self):
        manifest = self.build()
        lines, exec_line, argv = self.launcher_argv(manifest["outputRoot"])
        self.assertEqual(lines[0], "#!/bin/sh")
        self.assertEqual(
            argv,
            [
                "/usr/bin/python3",
                "-B",
                manifest["entryScript"],
                "--config",
                manifest["configPath"],
                "--allowed-root",
                str(self.root),
                "--expected-board-sha256",
                self.board_sha,
                "--expected-config-sha256",
                manifest["configSha256"],
            ],
        )
        self.assertEqual(exec_line, "exec " + " ".join(shlex.quote(part) for part in argv))
        # 空格与单引号路径必须被真正引用。
        self.assertIn(" ", manifest["configPath"])
        self.assertIn("'", manifest["configPath"])
        self.assertIn("'", exec_line)

    def test_launcher_has_no_free_command_or_expansion(self):
        manifest = self.build()
        raw = (Path(manifest["outputRoot"]) / "D4.app/Contents/MacOS/D4").read_bytes().decode()
        for forbidden in ("$", "`", "eval", "source", "&&", "||", ";"):
            self.assertNotIn(forbidden, raw, forbidden)
        for forbidden in ("launchctl", "bootstrap", "bootout", "kickstart"):
            self.assertNotIn(forbidden, raw, forbidden)
        self.assertEqual(len(raw.splitlines()), 3)

    def test_command_shortcut_matches_launcher(self):
        manifest = self.build()
        out = Path(manifest["outputRoot"])
        self.assertEqual(
            (out / "D4.command").read_bytes(),
            (out / "D4.app/Contents/MacOS/D4").read_bytes(),
        )
        _lines, _exec_line, argv = self.launcher_argv(manifest["outputRoot"], "D4.command")
        self.assertEqual(argv[0], "/usr/bin/python3")

    def test_command_shortcut_can_be_omitted(self):
        manifest = self.build(command_shortcut=False)
        out = Path(manifest["outputRoot"])
        self.assertFalse((out / "D4.command").exists())
        self.assertNotIn("D4.command", [item["path"] for item in manifest["files"]])
        self.assertEqual(
            sorted(os.listdir(out)),
            ["D4.app", "com.conductor.d4.board.plist", "config.json"],
        )

    def test_launcher_argv_is_correct_for_spaces_and_quotes_in_all_pinned_paths(self):
        # 把输出名再叠加一个双引号，确认转义覆盖双引号场景。
        manifest = self.build(output_name='D4 "quoted" package')
        _lines, exec_line, argv = self.launcher_argv(manifest["outputRoot"])
        self.assertIn('"', manifest["configPath"])
        self.assertEqual(shlex.split(exec_line)[1:], argv)
        self.assertEqual(argv[1:3], ["-B", manifest["entryScript"]])
        self.assertEqual(argv[argv.index("--config") + 1], manifest["configPath"])


# --------------------------------------------------------------------------- #
# 权限
# --------------------------------------------------------------------------- #
@requires_python
class PermissionTests(PackageFixtureTestCase):
    def test_modes_are_fixed(self):
        manifest = self.build()
        out = Path(manifest["outputRoot"])
        expectations = {
            "D4.app/Contents/MacOS/D4": 0o755,
            "D4.command": 0o755,
            "config.json": 0o600,
            "com.conductor.d4.board.plist": 0o600,
            "D4.app/Contents/Info.plist": 0o600,
            "D4.app/Contents/Resources/D4.icns": 0o644,
        }
        for relative, mode in expectations.items():
            with self.subTest(relative=relative):
                self.assertEqual(
                    stat.S_IMODE(os.lstat(out / relative).st_mode), mode, relative
                )

    def test_launchers_are_executable_and_payloads_are_not(self):
        manifest = self.build()
        out = Path(manifest["outputRoot"])
        self.assertTrue(os.access(out / "D4.app/Contents/MacOS/D4", os.X_OK))
        self.assertTrue(os.access(out / "D4.command", os.X_OK))
        self.assertFalse(os.access(out / "config.json", os.X_OK))
        self.assertFalse(os.access(out / "com.conductor.d4.board.plist", os.X_OK))

    def test_manifest_reports_the_same_modes(self):
        manifest = self.build()
        by_path = {item["path"]: item["mode"] for item in manifest["files"]}
        self.assertEqual(by_path["D4.app/Contents/MacOS/D4"], "0755")
        self.assertEqual(by_path["D4.command"], "0755")
        self.assertEqual(by_path["config.json"], "0600")
        self.assertEqual(by_path["com.conductor.d4.board.plist"], "0600")
        self.assertEqual(by_path["D4.app/Contents/Info.plist"], "0600")
        self.assertEqual(by_path["D4.app/Contents/Resources/D4.icns"], "0644")


# --------------------------------------------------------------------------- #
# config.json 的规范字节与指纹
# --------------------------------------------------------------------------- #
@requires_python
class ConfigPinTests(PackageFixtureTestCase):
    def test_config_bytes_are_canonical_and_bounded(self):
        config = self.prepare_ready()
        manifest = self.build(config=config)
        raw = Path(manifest["configPath"]).read_bytes()
        expected = json.dumps(
            config, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        self.assertEqual(raw, expected)
        self.assertLessEqual(len(raw), pkg.MAX_CONFIG_BYTES)

    def test_config_can_be_read_by_the_accepted_entry(self):
        config = self.prepare_ready()
        manifest = self.build(config=config)
        raw = Path(manifest["configPath"]).read_bytes()
        self.assertTrue(entry._is_lower_hex64(manifest["configSha256"]))
        self.assertEqual(hashlib.sha256(raw).hexdigest(), manifest["configSha256"])
        mapping = entry._strict_json_object(raw.decode("utf-8"))
        self.assertEqual(mapping, config)
        validated = appcfg.validate_config(
            mapping, allowed_root=str(self.root), expected_board_sha256=self.board_sha
        )
        self.assertEqual(validated.board_script, str(self.board_script))

    def test_launcher_pins_the_written_config_digest(self):
        manifest = self.build()
        _lines, _exec_line, argv = self.launcher_argv(manifest["outputRoot"])
        pinned = argv[argv.index("--expected-config-sha256") + 1]
        self.assertEqual(
            pinned, hashlib.sha256(Path(manifest["configPath"]).read_bytes()).hexdigest()
        )
        self.assertEqual(
            argv[argv.index("--expected-board-sha256") + 1], self.board_sha
        )

    def test_config_with_space_and_quote_paths_round_trips(self):
        config = self.prepare_ready()
        manifest = self.build(config=config)
        raw = Path(manifest["configPath"]).read_bytes()
        mapping = json.loads(raw.decode("utf-8"))
        self.assertEqual(mapping["dbPath"], str(self.db_path))
        self.assertIn("'", str(self.db_path))
        self.assertEqual(mapping["runtimeDir"], str(self.runtime_dir))


# --------------------------------------------------------------------------- #
# 不覆盖 / 不越界
# --------------------------------------------------------------------------- #
@requires_python
class NoOverwriteTests(PackageFixtureTestCase):
    def test_existing_output_directory_is_refused_and_untouched(self):
        self.prepare_ready()
        existing = self.artifact_root / OUTPUT_NAME
        existing.mkdir()
        sentinel = existing / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        before = _fingerprint([existing, sentinel])
        with self.assertRaises(pkg.PackageError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(_fingerprint([existing, sentinel]), before)
        self.assertEqual(sorted(os.listdir(existing)), ["keep.txt"])

    def test_existing_output_file_is_refused(self):
        self.prepare_ready()
        target = self.artifact_root / OUTPUT_NAME
        target.write_text("keep", encoding="utf-8")
        with self.assertRaises(pkg.PackageError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(target.read_text(encoding="utf-8"), "keep")

    @requires_symlink
    def test_existing_symlink_output_is_refused_without_following_it(self):
        self.prepare_ready()
        redirect = self.root / "redirect"
        redirect.mkdir()
        (redirect / "payload.txt").write_text("keep", encoding="utf-8")
        link = self.artifact_root / OUTPUT_NAME
        os.symlink(str(redirect), str(link))
        with self.assertRaises(pkg.PackageError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(sorted(os.listdir(redirect)), ["payload.txt"])
        self.assertTrue(os.path.islink(link))


@requires_python
class OutputScopeTests(PackageFixtureTestCase):
    def test_output_name_must_be_a_single_safe_segment(self):
        self.prepare_ready()
        for name in ("", ".", "..", "a/b", "/absolute", "nested/child", "a\x00b", "a\nb"):
            with self.subTest(name=repr(name)):
                with self.assertRaises(pkg.PackageError) as caught:
                    self.build(output_name=name)
                self.assertEqual(caught.exception.code, "output_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_temporary_artifact_root_is_refused(self):
        self.prepare_ready()
        for root in ("/tmp", "/private/tmp", "/var/folders/aa/bb", "/private/var/folders/aa/bb"):
            with self.subTest(root=root):
                with self.assertRaises(pkg.PackageError) as caught:
                    self.build(artifact_root=root)
                self.assertEqual(caught.exception.code, "artifact_root_invalid")

    def test_installation_locations_are_refused(self):
        self.prepare_ready()
        home = os.path.expanduser("~")
        for root in (
            home + "/Library/LaunchAgents",
            home + "/Applications",
            "/Library/LaunchAgents",
            "/Applications",
        ):
            with self.subTest(root=root):
                with self.assertRaises(pkg.PackageError) as caught:
                    self.build(artifact_root=root)
                self.assertEqual(caught.exception.code, "artifact_root_invalid")

    def test_relative_or_missing_artifact_root_is_refused(self):
        self.prepare_ready()
        for root in ("relative/root", str(self.root / "missing-artifacts")):
            with self.subTest(root=root):
                with self.assertRaises(pkg.PackageError) as caught:
                    self.build(artifact_root=root)
                self.assertEqual(caught.exception.code, "artifact_root_invalid")

    @requires_symlink
    def test_symlinked_artifact_root_is_refused(self):
        self.prepare_ready()
        alias = self.root / "artifact-alias"
        os.symlink(str(self.artifact_root), str(alias))
        with self.assertRaises(pkg.PackageError) as caught:
            self.build(artifact_root=alias)
        self.assertEqual(caught.exception.code, "artifact_root_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_no_output_is_created_before_validation(self):
        # 非 ready 来源必须在创建输出目录之前就失败。
        self.write_database(schema=False)
        self.write_catalog()
        with self.assertRaises(pkg.PackageError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "source_not_ready")
        self.assertEqual(os.listdir(self.artifact_root), [])


# --------------------------------------------------------------------------- #
# 来源不可用 / 入口非法
# --------------------------------------------------------------------------- #
@requires_python
class RefuseInvalidSourceTests(PackageFixtureTestCase):
    def test_unready_database_is_refused(self):
        self.write_database(schema=False)
        self.write_catalog()
        with self.assertRaises(pkg.PackageError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "source_not_ready")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_missing_source_is_refused(self):
        self.db_path.unlink()
        self.write_catalog()
        with self.assertRaises(pkg.PackageError) as caught:
            self.build()
        self.assertEqual(caught.exception.code, "config_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_board_sha_mismatch_is_refused(self):
        self.prepare_ready()
        with self.assertRaises(pkg.PackageError) as caught:
            pkg.build_package(
                self.config(),
                allowed_root=str(self.root),
                expected_board_sha256="0" * 64,
                artifact_root=str(self.artifact_root),
                output_name="nope",
            )
        self.assertEqual(caught.exception.code, "config_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_changed_board_script_is_refused(self):
        config = self.prepare_ready()
        self.build(config=config)
        self.board_script.write_text("changed board content\n", encoding="utf-8")
        with self.assertRaises(pkg.PackageError) as caught:
            self.build(config=config, output_name="after-change")
        self.assertEqual(caught.exception.code, "config_invalid")
        self.assertNotIn("after-change", os.listdir(self.artifact_root))

    def test_invalid_config_mappings_are_refused(self):
        self.prepare_ready()
        cases = [
            {},
            {"only": "one-field"},
            {key: value for key, value in self.config().items() if key != "runtimeDir"},
            dict(self.config(), logDir="/tmp/logs"),
            self.config(dbPath=None, catalogPath=None),
        ]
        for index, case in enumerate(cases):
            with self.subTest(case=index):
                with self.assertRaises(pkg.PackageError) as caught:
                    self.build(config=case, output_name="case-%d" % index)
                self.assertEqual(caught.exception.code, "config_invalid")
        # 显式 None（非“用默认值”语义）也必须被拒。
        with self.assertRaises(pkg.PackageError) as caught:
            pkg.build_package(
                None,
                allowed_root=str(self.root),
                expected_board_sha256=self.board_sha,
                artifact_root=str(self.artifact_root),
                output_name="case-none",
            )
        self.assertEqual(caught.exception.code, "config_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_invalid_entry_script_is_refused(self):
        self.prepare_ready()
        with self.assertRaises(pkg.PackageError) as caught:
            self.build(entry_script=str(self.root / "missing-entry.py"))
        self.assertEqual(caught.exception.code, "entry_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_entry_digest_pin_must_match(self):
        self.prepare_ready()
        with self.assertRaises(pkg.PackageError) as caught:
            self.build(expected_entry_sha256="0" * 64)
        self.assertEqual(caught.exception.code, "entry_invalid")
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_entry_digest_pin_accepts_the_accepted_entry(self):
        self.prepare_ready()
        entry_path = _SCRIPTS_DIR / "d4_app_entry.py"
        digest = hashlib.sha256(entry_path.read_bytes()).hexdigest()
        manifest = self.build(expected_entry_sha256=digest)
        self.assertEqual(manifest["entrySha256"], digest)

    def test_non_boolean_command_shortcut_flag_is_refused(self):
        self.prepare_ready()
        with self.assertRaises(pkg.PackageError) as caught:
            self.build(command_shortcut="yes")
        self.assertEqual(caught.exception.code, "invalid_arguments")


# --------------------------------------------------------------------------- #
# 不改动输入 / 事务回滚
# --------------------------------------------------------------------------- #
@requires_python
class NoInputChangeTests(PackageFixtureTestCase):
    def _watched(self):
        logs = self.runtime_dir / "logs"
        return [
            self.db_path,
            self.catalog_path,
            self.board_script,
            self.runtime_dir,
            logs,
            logs / "board.out.log",
            logs / "board.err.log",
            Path(str(self.db_path) + "-wal"),
            Path(str(self.db_path) + "-journal"),
        ]

    def test_successful_build_changes_no_input(self):
        config = self.prepare_ready()
        db_bytes = self.db_path.read_bytes()
        before = _fingerprint(self._watched())
        manifest = self.build(config=config)
        self.assertEqual(_fingerprint(self._watched()), before)
        self.assertEqual(self.db_path.read_bytes(), db_bytes)
        self.assertEqual(sorted(os.listdir(self.runtime_dir / "logs")), [])
        self.assertEqual(sorted(os.listdir(self.artifact_root)), [OUTPUT_NAME])
        self.assertTrue((Path(manifest["outputRoot"]) / "config.json").is_file())

    def test_failed_source_validation_leaves_everything_untouched(self):
        self.write_database(schema=False)
        self.write_catalog()
        before = _fingerprint(self._watched())
        with self.assertRaises(pkg.PackageError):
            self.build()
        self.assertEqual(_fingerprint(self._watched()), before)
        self.assertEqual(os.listdir(self.artifact_root), [])

    def test_partial_construction_is_rolled_back_and_neighbours_kept(self):
        config = self.prepare_ready()
        neighbour = self.artifact_root / "neighbour"
        neighbour.mkdir()
        (neighbour / "keep.txt").write_text("keep", encoding="utf-8")

        original = pkg._write_new_file
        calls = {"count": 0}

        def flaky(path, data, mode):
            calls["count"] += 1
            if calls["count"] >= 3:
                raise OSError("synthetic construction failure")
            return original(path, data, mode)

        with mock.patch.object(pkg, "_write_new_file", side_effect=flaky):
            with self.assertRaises(pkg.PackageError) as caught:
                self.build(config=config)
        self.assertEqual(caught.exception.code, "construction_failed")
        self.assertFalse((self.artifact_root / OUTPUT_NAME).exists())
        self.assertEqual(sorted(os.listdir(self.artifact_root)), ["neighbour"])
        self.assertEqual((neighbour / "keep.txt").read_text(encoding="utf-8"), "keep")
        self.assertGreaterEqual(calls["count"], 3)

    def test_rebuilding_the_same_name_is_byte_identical(self):
        config = self.prepare_ready()
        first = self.build(config=config)
        captured = {item["path"]: item["sha256"] for item in first["files"]}
        shutil.rmtree(first["outputRoot"])
        second = self.build(config=config)
        self.assertEqual(
            {item["path"]: item["sha256"] for item in second["files"]}, captured
        )


# --------------------------------------------------------------------------- #
# 无进程 / 无网络
# --------------------------------------------------------------------------- #
@requires_python
class BuildIsolationTests(PackageFixtureTestCase):
    def test_build_uses_no_process_and_no_network(self):
        config = self.prepare_ready()
        with mock.patch("socket.socket", side_effect=AssertionError("network trap")) as net, \
                mock.patch("subprocess.Popen", side_effect=AssertionError("process trap")) as popen, \
                mock.patch("subprocess.run", side_effect=AssertionError("process trap")) as run, \
                mock.patch("os.system", side_effect=AssertionError("shell trap")) as shell:
            self.build(config=config)
        self.assertFalse(net.called)
        self.assertFalse(popen.called)
        self.assertFalse(run.called)
        self.assertFalse(shell.called)


class StaticSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.source = (_SCRIPTS_DIR / "d4_app_package.py").read_text(encoding="utf-8")
        self.tree = ast.parse(self.source)

    def test_no_network_or_process_imports(self):
        forbidden = {
            "socket", "subprocess", "http", "urllib", "ssl", "ftplib", "asyncio",
            "multiprocessing", "ctypes", "requests", "webbrowser", "launchd",
        }
        imported = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertEqual(imported & forbidden, set())

    def test_no_shell_service_or_install_token_in_source(self):
        for token in ("launchctl", "bootstrap", "bootout", "kickstart", "os.system", "popen"):
            self.assertNotIn(token, self.source, token)

    def test_no_command_line_entry_point(self):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.If):
                names = {child.id for child in ast.walk(node.test) if isinstance(child, ast.Name)}
                self.assertNotIn("__name__", names, "候选模块不得包含 __main__ 入口")

    def test_public_api_is_exactly_the_contract(self):
        self.assertTrue(callable(pkg.build_package))
        self.assertTrue(callable(pkg.build_icns))
        self.assertTrue(issubclass(pkg.PackageError, Exception))
        self.assertEqual(sorted(pkg.__all__), ["PackageError", "build_icns", "build_package"])

    def test_manifest_error_codes_are_bounded(self):
        for code in pkg.PACKAGE_ERROR_CODES:
            self.assertEqual(str(pkg.PackageError(code)), code)
        with self.assertRaises(ValueError):
            pkg.PackageError("definitely-not-a-code")


# --------------------------------------------------------------------------- #
# 合成“服务重启”：重载模块/身份 + 只读自有 fixture 库
# --------------------------------------------------------------------------- #
@requires_python
class SyntheticRestartTests(PackageFixtureTestCase):
    def test_reloading_accepted_modules_and_reprobing_leaves_data_unchanged(self):
        config = self.prepare_ready()
        db_before = self.db_path.read_bytes()
        board_before = self.board_script.read_bytes()
        catalog_before = self.catalog_path.read_bytes()
        before = _fingerprint(self._watched_paths())

        manifest = self.build(config=config)
        self.assertTrue((Path(manifest["outputRoot"]) / "config.json").is_file())

        # 重新加载“已接受”的运行时模块并按冻结身份重新探测：相当于合成一次服务重启。
        reloaded = _load("d4_health_runtime_reload", "d4_health_runtime.py")
        frozen = reloaded.freeze_identity(config, expected_board_sha256=self.board_sha)
        payload = reloaded.collect_health(frozen)
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["reason"], "ok")

        # 只读读取自有 fixture 库证明数据未变。
        self.assertEqual(_fingerprint(self._watched_paths()), before)
        self.assertEqual(self.db_path.read_bytes(), db_before)
        self.assertEqual(self.board_script.read_bytes(), board_before)
        self.assertEqual(self.catalog_path.read_bytes(), catalog_before)
        self.assertEqual(sorted(os.listdir(self.runtime_dir / "logs")), [])
        # 真实 macOS 加载 / 双击 / 登录重启 / 崩溃恢复行为：NOT_RUN（本 worker 未执行）。

    def _watched_paths(self):
        logs = self.runtime_dir / "logs"
        return [
            self.db_path,
            self.catalog_path,
            self.board_script,
            logs / "board.out.log",
            logs / "board.err.log",
            Path(str(self.db_path) + "-wal"),
        ]


if __name__ == "__main__":
    unittest.main()
