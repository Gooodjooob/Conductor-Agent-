"""D4 看板离线 App 打包候选（G2-D：只生成文件，不安装、不启动、不联网）。

职责
----
``build_package(...)`` 在一个**由调用方显式指定的全新输出目录**里生成可离线分发的
macOS App 候选包：

* ``D4.app/Contents/Info.plist``（正确的可执行文件 / 图标 / 标识 / 名称 / 包类型）；
* ``D4.app/Contents/MacOS/D4``（``/bin/sh`` 启动脚本，权限 0755）；
* ``D4.app/Contents/Resources/D4.icns``（标准库确定性生成的图标）；
* ``config.json``（固定字节指纹的九字段配置，权限 0600）；
* ``com.conductor.d4.board.plist``（launchd 候选，只拷贝 G1 生成器字节，权限 0600）；
* 同级 ``D4.command``（可选的 ``/bin/sh`` 快捷方式，权限 0755）。

顺序与边界（fail-closed）
-----------------------
* 先做 G1 校验与 freeze-readiness：``d4_app_config.validate_config``（``allowed_root``、
  ``boardScript`` 的 sha256、端口、来源路径）随后 ``d4_health_runtime.freeze_identity`` +
  ``collect_health``；只有 ``ready is True`` 才继续。任何一步失败都在**创建输出之前**
  抛出通用错误码，绝不留下半成品。
* 输出目录只允许位于调用方受信 ``artifact_root`` 之下、名字是单个安全路径段、且**不存在**
  （不覆盖任何已有文件 / 目录 / 符号链接）。临时目录与 ``~/Applications``、
  ``~/Library/LaunchAgents`` 这类安装位置一律拒绝。
* 只写本次调用自己新建的输出目录；构造过程中任何失败都会回滚删除**本次调用新建的**那个
  输出目录，绝不触碰其他既有文件或同级邻居。
* 不安装、不加载、不启停任何服务或进程，不发起网络请求，不写运行时目录 / 数据库 / 日志：
  launchd plist 只是把 G1 生成器（``d4_app_config.build_launchd_plist``）的字节原样拷贝到输出。
* 启动脚本是固定 ``/bin/sh`` 加一条 ``exec`` 行：固定的 ``/usr/bin/python3 -B``、已接受的
  ``d4_app_entry.py`` 路径、``--config`` / ``--allowed-root`` / ``--expected-board-sha256`` /
  ``--expected-config-sha256``，所有路径与参数都用 ``shlex.quote`` 转义；不提供动态求值、
  环境变量覆盖或自由命令。
* 图标 ``D4.icns`` 只用标准库确定性生成：合法 ICNS 头与长度 + 128×128 PNG 表示，图案是位图
  字形拼出的 “D4” 标签；不依赖任何外部资源。
* 源脚本（``d4_app_entry.py``）按当前候选约定路径固定引用，不做搬迁承诺，也不被复制进输出包。

诚实边界（本卡未实现、未验证）
-----------------------------
* 不安装、不迁移、不加载服务；真实 macOS 双击 / 登录 / 重启 / 崩溃行为均为 NOT_RUN。
* 不实现备份 / 恢复工具，只在使用说明里给出离线只读恢复步骤（G2-E 另行跟进）。
* 不是完整 MVP：本模块只负责“生成候选包文件”，运行行为、服务注册与发布都需另行授权。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import plistlib
import shlex
import shutil
import stat
import struct
import sys
import unicodedata
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["PackageError", "build_icns", "build_package"]


# --------------------------------------------------------------------------- #
# Frozen sibling modules (loaded by exact path; sys.modules registered first)
# --------------------------------------------------------------------------- #
def _load_sibling(name: str, filename: str):
    """按精确文件路径加载一个同级候选模块（不扫描目录）。

    ``sys.modules`` 在 ``exec_module`` **之前**登记，使被加载模块里的模块级
    dataclass（``AppConfig``、``FrozenIdentity``）能通过已注册的模块条目解析
    ``__module__``。
    """
    path = Path(__file__).resolve().parent / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError("cannot load %s" % filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_APP_CONFIG = _load_sibling("d4_app_config", "d4_app_config.py")
_HEALTH_RUNTIME = _load_sibling("d4_health_runtime", "d4_health_runtime.py")


# --------------------------------------------------------------------------- #
# Fixed literals
# --------------------------------------------------------------------------- #
PACKAGE_MANIFEST_SCHEMA = "d4-app-package-manifest.v1"

APP_BUNDLE_NAME = "D4.app"
APP_EXECUTABLE_NAME = "D4"
APP_ICON_NAME = "D4.icns"
APP_BUNDLE_IDENTIFIER = _APP_CONFIG.LAUNCH_AGENT_LABEL
LAUNCH_AGENT_LABEL = _APP_CONFIG.LAUNCH_AGENT_LABEL
LAUNCHD_PLIST_NAME = _APP_CONFIG.LAUNCH_AGENT_LABEL + ".plist"
COMMAND_SHORTCUT_NAME = "D4.command"
CONFIG_NAME = "config.json"
ENTRY_SCRIPT_NAME = "d4_app_entry.py"

LAUNCHER_SHELL = "/bin/sh"
PYTHON_EXECUTABLE = _APP_CONFIG.ALLOWED_PYTHON_PATH

MODE_LAUNCHER = 0o755
MODE_CONFIG = 0o600
MODE_PLIST = 0o600
MODE_ICON = 0o644

MAX_CONFIG_BYTES = 16 * 1024
MAX_OUTPUT_FILE_BYTES = 8 * 1024 * 1024
READ_CHUNK = 65536

# 输出位置禁区：临时根目录与各类安装位置（用户级/系统级 Applications、LaunchAgents）。
FORBIDDEN_SEGMENT_NAMES = frozenset({"LaunchAgents"})
FORBIDDEN_PATH_PATTERNS = (
    "~/Applications",
    "~/Library/LaunchAgents",
    "/Applications",
    "/Library/LaunchAgents",
)

PACKAGE_ERROR_CODES = (
    "invalid_arguments",
    "config_invalid",
    "source_not_ready",
    "entry_invalid",
    "artifact_root_invalid",
    "output_invalid",
    "output_exists",
    "construction_failed",
)

# ---- 确定性图标参数（标准库生成，无外部资源） ---- #
ICON_SIZE = 128
ICNS_MAGIC = b"icns"
ICNS_PNG_TYPE_128 = b"ic07"
ICON_BACKGROUND = (0x1F, 0x3A, 0x5F, 0xFF)
ICON_FOREGROUND = (0xFF, 0xFF, 0xFF, 0xFF)
ICON_LABEL = "D4"
GLYPH_WIDTH = 5
GLYPH_HEIGHT = 7
ICON_SCALE = 10
ICON_GAP = 10
GLYPHS = {
    "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
    "4": ("00100", "01100", "10100", "11111", "00100", "00100", "00100"),
}

_LOWER_HEX = frozenset("0123456789abcdef")


# --------------------------------------------------------------------------- #
# Public error type: bounded codes only
# --------------------------------------------------------------------------- #
class PackageError(Exception):
    """打包失败时抛出的通用错误，只携带一个固定错误码。

    ``str(error)`` 就是错误码本身：路径、异常正文、来源内容都不会进入调用方或报告。
    """

    def __init__(self, code: str) -> None:
        if code not in PACKAGE_ERROR_CODES:
            raise ValueError("invalid package error code")
        self.code = code
        super().__init__(code)


# --------------------------------------------------------------------------- #
# Scalar / path helpers
# --------------------------------------------------------------------------- #
def _lstat(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _string_value(value: Any, code: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or value == "":
        raise PackageError(code)
    for char in value:
        if char == "\x00" or unicodedata.category(char).startswith("C"):
            raise PackageError(code)
    return value


def _absolute_canonical(value: Any, code: str) -> str:
    text = _string_value(value, code)
    if not text.startswith("/"):
        raise PackageError(code)
    for part in text.split("/")[1:]:
        if part in ("", ".", ".."):
            raise PackageError(code)
    return text


def _reject_temporary(path: str, code: str) -> str:
    for prefix in _APP_CONFIG.TEMPORARY_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            raise PackageError(code)
    return path


def _reject_forbidden_target(path: str, code: str) -> str:
    home = os.path.expanduser("~")
    for pattern in FORBIDDEN_PATH_PATTERNS:
        prefix = home + pattern[1:] if pattern.startswith("~") else pattern
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            raise PackageError(code)
    for part in path.split("/")[1:]:
        if part in FORBIDDEN_SEGMENT_NAMES:
            raise PackageError(code)
    return path


def _reject_symlink_components(path: str, code: str) -> str:
    current = ""
    for part in path.split("/")[1:]:
        current = current + "/" + part
        if os.path.islink(current):
            raise PackageError(code)
    return path


def _require_directory(path: str, code: str) -> str:
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise PackageError(code)
    return path


def _read_bounded_file(path: str, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise PackageError("construction_failed") from None
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise PackageError("construction_failed")
        chunks: List[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise PackageError("construction_failed")
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        raise PackageError("construction_failed") from None
    finally:
        os.close(descriptor)


def _sha256_of_file(path: str, code: str) -> str:
    try:
        raw = _read_bounded_file(path, MAX_OUTPUT_FILE_BYTES)
    except PackageError:
        raise PackageError(code) from None
    return hashlib.sha256(raw).hexdigest()


# --------------------------------------------------------------------------- #
# Step 1: G1 校验 + freeze-readiness（全部在创建输出之前完成）
# --------------------------------------------------------------------------- #
def _validate_source(config: Any, allowed_root: Any, expected_board_sha256: Any):
    """用 G1 校验配置/root/board SHA，再用冻结运行时确认来源就绪。"""
    try:
        validated = _APP_CONFIG.validate_config(
            config,
            allowed_root=allowed_root,
            expected_board_sha256=expected_board_sha256,
        )
    except (ValueError, TypeError, OSError):
        raise PackageError("config_invalid") from None

    try:
        frozen = _HEALTH_RUNTIME.freeze_identity(
            validated, expected_board_sha256=expected_board_sha256
        )
    except _HEALTH_RUNTIME.HealthRuntimeError:
        raise PackageError("source_not_ready") from None

    try:
        payload = _HEALTH_RUNTIME.collect_health(frozen)
    except _HEALTH_RUNTIME.HealthRuntimeError:
        raise PackageError("source_not_ready") from None

    if payload.get("ready") is not True:
        raise PackageError("source_not_ready")
    return validated


# --------------------------------------------------------------------------- #
# Step 2: 入口脚本与输出位置
# --------------------------------------------------------------------------- #
def _resolve_entry(entry_script: Any, expected_entry_sha256: Any) -> Tuple[str, str]:
    """确定被启动的 ``d4_app_entry.py`` 路径，并记录（可选校验）其 sha256。"""
    path = str(Path(__file__).resolve().parent / ENTRY_SCRIPT_NAME)
    if entry_script is not None and _absolute_canonical(entry_script, "entry_invalid") != path:
        raise PackageError("entry_invalid")
    _reject_symlink_components(path, "entry_invalid")

    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise PackageError("entry_invalid")
    digest = _sha256_of_file(path, "entry_invalid")

    if expected_entry_sha256 is not None:
        expected = _string_value(expected_entry_sha256, "entry_invalid")
        if len(expected) != 64 or any(char not in _LOWER_HEX for char in expected):
            raise PackageError("entry_invalid")
        if digest != expected:
            raise PackageError("entry_invalid")
    return path, digest


def _validate_artifact_root(value: Any) -> str:
    path = _absolute_canonical(value, "artifact_root_invalid")
    _reject_temporary(path, "artifact_root_invalid")
    _reject_forbidden_target(path, "artifact_root_invalid")
    _reject_symlink_components(path, "artifact_root_invalid")
    return _require_directory(path, "artifact_root_invalid")


def _validate_output_name(value: Any) -> str:
    text = _string_value(value, "output_invalid")
    if text in (".", "..") or "/" in text or text.startswith("."):
        raise PackageError("output_invalid")
    return text


# --------------------------------------------------------------------------- #
# Step 3: 包内容（全部确定性、纯内存构造，不产生 I/O）
# --------------------------------------------------------------------------- #
def _serialize_config(validated: Any) -> bytes:
    """把已校验的 AppConfig 序列化为九字段的规范 JSON 字节（有界）。"""
    document = {
        name: getattr(validated, attribute)
        for name, attribute in zip(
            _HEALTH_RUNTIME.CONFIG_FIELDS, _HEALTH_RUNTIME.CONFIG_ATTRIBUTES
        )
    }
    try:
        raw = json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise PackageError("config_invalid") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise PackageError("config_invalid")
    return raw


def _launch_script(
    entry_path: str,
    config_path: str,
    allowed_root: str,
    board_sha256: str,
    config_sha256: str,
) -> bytes:
    """生成 ``/bin/sh`` + 单条 ``exec`` 的启动脚本字节（参数经 ``shlex.quote``）。"""
    argv = (
        PYTHON_EXECUTABLE,
        "-B",
        entry_path,
        "--config",
        config_path,
        "--allowed-root",
        allowed_root,
        "--expected-board-sha256",
        board_sha256,
        "--expected-config-sha256",
        config_sha256,
    )
    line = "exec " + " ".join(shlex.quote(part) for part in argv)
    text = (
        "#!" + LAUNCHER_SHELL + "\n"
        "# generated by d4_app_package (deterministic). Do not edit by hand.\n"
        + line
        + "\n"
    )
    return text.encode("utf-8")


def _info_plist_bytes() -> bytes:
    payload = {
        "CFBundleName": APP_EXECUTABLE_NAME,
        "CFBundleDisplayName": APP_EXECUTABLE_NAME,
        "CFBundleIdentifier": APP_BUNDLE_IDENTIFIER,
        "CFBundleExecutable": APP_EXECUTABLE_NAME,
        "CFBundleIconFile": APP_ICON_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleInfoDictionaryVersion": "6.0",
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML)


def _render_label(size: int = ICON_SIZE, label: str = ICON_LABEL) -> List[bytearray]:
    """把 “D4” 位图字形按固定比例渲染成 RGBA 行（纯函数，确定性）。"""
    background = bytes(ICON_BACKGROUND)
    foreground = bytes(ICON_FOREGROUND)
    rows = [bytearray(background * size) for _ in range(size)]

    glyph_width = GLYPH_WIDTH * ICON_SCALE
    glyph_height = GLYPH_HEIGHT * ICON_SCALE
    total_width = len(label) * glyph_width + (len(label) - 1) * ICON_GAP
    origin_x = (size - total_width) // 2
    origin_y = (size - glyph_height) // 2

    for index, character in enumerate(label):
        pattern = GLYPHS[character]
        base_x = origin_x + index * (glyph_width + ICON_GAP)
        for row_index, line in enumerate(pattern):
            for column_index, flag in enumerate(line):
                if flag != "1":
                    continue
                left = (base_x + column_index * ICON_SCALE) * 4
                for depth in range(ICON_SCALE):
                    row = rows[origin_y + row_index * ICON_SCALE + depth]
                    row[left:left + 4 * ICON_SCALE] = foreground * ICON_SCALE
    return rows


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _encode_png(rows: List[bytearray], width: int, height: int) -> bytes:
    raw = b"".join(b"\x00" + bytes(row) for row in rows)
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


def build_icns(size: int = ICON_SIZE) -> bytes:
    """确定性生成 ICNS 字节：合法头/长度 + 128×128 PNG 表示（“D4” 标签）。"""
    png = _encode_png(_render_label(size), size, size)
    body = ICNS_PNG_TYPE_128 + struct.pack(">I", 8 + len(png)) + png
    return ICNS_MAGIC + struct.pack(">I", 8 + len(body)) + body


# --------------------------------------------------------------------------- #
# Step 4: 事务式写入
# --------------------------------------------------------------------------- #
def _make_directories(base: str, directory: str) -> None:
    relative = directory[len(base) + 1:]
    current = base
    for part in relative.split("/"):
        current = current + "/" + part
        if not os.path.isdir(current):
            os.mkdir(current, 0o755)
        os.chmod(current, 0o755)


def _write_new_file(path: str, data: bytes, mode: int) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        view = memoryview(data)
        while len(view) > 0:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write")
            view = view[written:]
    finally:
        os.close(descriptor)
    os.chmod(path, mode)


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def build_package(
    config: Any,
    *,
    allowed_root: Any,
    expected_board_sha256: Any,
    artifact_root: Any,
    output_name: Any,
    entry_script: Any = None,
    expected_entry_sha256: Any = None,
    command_shortcut: bool = True,
) -> Dict[str, Any]:
    """在受信 ``artifact_root`` 下新建 ``output_name`` 目录并生成离线 App 候选包。

    顺序固定且 fail-closed：先 G1 校验 + freeze-readiness（不写任何东西），再确定入口脚本、
    校验输出位置（必须不存在）、构造全部确定性字节，最后才新建输出目录并写入。构造期间
    任何失败都会删除**本次调用新建的**输出目录。

    返回生成的 manifest（文件相对路径 / sha256 / 字节数 / 权限，以及 config、board、
    entry 的 sha256 身份）。失败抛 :class:`PackageError`，只带一个固定错误码。
    """
    if not isinstance(command_shortcut, bool):
        raise PackageError("invalid_arguments")

    validated = _validate_source(config, allowed_root, expected_board_sha256)
    entry_path, entry_digest = _resolve_entry(entry_script, expected_entry_sha256)
    root = _validate_artifact_root(artifact_root)
    name = _validate_output_name(output_name)

    output_dir = root + "/" + name
    if os.path.lexists(output_dir):
        raise PackageError("output_exists")

    # launchd plist 只取 G1 生成器字节（要求 runtimeDir/logs 已存在），本函数不做任何安装。
    try:
        launchd_bytes = _APP_CONFIG.build_launchd_plist(validated)
    except (ValueError, TypeError, OSError):
        raise PackageError("source_not_ready") from None

    config_bytes = _serialize_config(validated)
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    config_path = output_dir + "/" + CONFIG_NAME

    launcher = _launch_script(
        entry_path, config_path, allowed_root, expected_board_sha256, config_digest
    )

    planned: List[Tuple[str, bytes, int]] = [
        (APP_BUNDLE_NAME + "/Contents/Info.plist", _info_plist_bytes(), MODE_PLIST),
        (APP_BUNDLE_NAME + "/Contents/MacOS/" + APP_EXECUTABLE_NAME, launcher, MODE_LAUNCHER),
        (APP_BUNDLE_NAME + "/Contents/Resources/" + APP_ICON_NAME, build_icns(), MODE_ICON),
        (CONFIG_NAME, config_bytes, MODE_CONFIG),
        (LAUNCHD_PLIST_NAME, launchd_bytes, MODE_PLIST),
    ]
    if command_shortcut:
        planned.append((COMMAND_SHORTCUT_NAME, launcher, MODE_LAUNCHER))

    created = False
    try:
        os.mkdir(output_dir, 0o755)
        created = True
        os.chmod(output_dir, 0o755)
        for relative, data, mode in planned:
            target = output_dir + "/" + relative
            directory = os.path.dirname(target)
            if directory != output_dir:
                _make_directories(output_dir, directory)
            _write_new_file(target, data, mode)

        files = []
        for relative, _data, _mode in planned:
            absolute = output_dir + "/" + relative
            digest = _sha256_of_file(absolute, "construction_failed")
            info = os.lstat(absolute)
            files.append({
                "path": relative,
                "sha256": digest,
                "bytes": os.lstat(absolute).st_size,
                "mode": "0%03o" % stat.S_IMODE(info.st_mode),
            })
        files.sort(key=lambda item: item["path"])
    except PackageError:
        if created:
            shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except BaseException:
        if created:
            shutil.rmtree(output_dir, ignore_errors=True)
        raise PackageError("construction_failed") from None

    return {
        "schemaVersion": PACKAGE_MANIFEST_SCHEMA,
        "outputName": name,
        "outputRoot": output_dir,
        "bundleName": APP_BUNDLE_NAME,
        "bundleIdentifier": APP_BUNDLE_IDENTIFIER,
        "launchAgentLabel": validated.label,
        "configPath": config_path,
        "configSha256": config_digest,
        "boardSha256": expected_board_sha256,
        "entryScript": entry_path,
        "entrySha256": entry_digest,
        "files": files,
    }
