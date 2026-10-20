"""D4 看板 App 配置校验与 launchd 载荷序列化候选（G1 合同冻结范围）。

职责（只有这三件事）：
1. ``validate_config``：把固定九字段配置映射校验为不可变 ``AppConfig``；
2. ``build_board_argv``：从 ``AppConfig`` 生成精确 argv 元组（每项独立，不拼接、不过 shell）；
   ``--runtime-dir`` 固定插在 sources（``--db``/``--catalog``）之后、``--port`` 之前，``--port`` 仍居末位；
3. ``build_launchd_plist``：从 ``AppConfig`` 生成 plist 字节（只返回字节，不落盘）。

边界：本模块不启动、不安装、不加载、不控制任何常驻服务；不发起任何网络请求；
不创建、写入或迁移数据库；没有命令行入口，也没有自由 argv/env 字段。
import 本模块没有任何 I/O、进程或服务副作用。

校验失败统一抛 ``ValueError``；builder 收到非 ``AppConfig``（例如 dict）时抛 ``TypeError``。
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import plistlib
import stat
import unicodedata
from collections.abc import Mapping
from typing import Any, Optional, Tuple

__all__ = [
    "AppConfig",
    "validate_config",
    "build_board_argv",
    "build_launchd_plist",
]

SCHEMA_VERSION = "d4-board-app-config.v1"
ALLOWED_HOST = "127.0.0.1"
ALLOWED_PYTHON_PATH = "/usr/bin/python3"
LAUNCH_AGENT_LABEL = "com.conductor.d4.board"
BOARD_SCRIPT_BASENAME = "d4_board.py"
PORT_MIN = 1024
PORT_MAX = 65535
THROTTLE_INTERVAL = 10
PROCESS_TYPE = "Background"
LOG_DIR_NAME = "logs"
STDOUT_LOG_NAME = "board.out.log"
STDERR_LOG_NAME = "board.err.log"
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
TEMPORARY_PREFIXES = ("/tmp", "/private/tmp", "/var/folders", "/private/var/folders")
_LOWER_HEX = frozenset("0123456789abcdef")
_READ_CHUNK = 65536


@dataclasses.dataclass(frozen=True)
class AppConfig:
    """``validate_config`` 产出的不可变配置（frozen dataclass，字段不可重新赋值）。"""

    schema_version: str
    host: str
    port: int
    db_path: Optional[str]
    catalog_path: Optional[str]
    python_path: str
    board_script: str
    runtime_dir: str
    label: str


def validate_config(config: Mapping[str, Any], *, allowed_root: str, expected_board_sha256: str) -> AppConfig:
    """校验九字段配置并返回不可变 ``AppConfig``；任何不合规输入一律抛 ``ValueError``。

    ``allowed_root`` 必须由调用方（cox）传入：绝对、已存在、非符号链接、非临时路径的受控目录。
    ``expected_board_sha256`` 必须是调用方受信传入的 64 位小写十六进制 sha256，配置不能自选。
    """
    root = _validate_allowed_root(allowed_root)
    expected_sha = _validate_expected_sha(expected_board_sha256)
    fields = _validate_field_set(config)

    schema_version = _validate_string(fields["schemaVersion"], "schemaVersion")
    if schema_version != SCHEMA_VERSION:
        raise ValueError("schemaVersion 必须是 %s" % SCHEMA_VERSION)

    host = _validate_string(fields["host"], "host")
    if host != ALLOWED_HOST:
        raise ValueError("host 只能是 %s" % ALLOWED_HOST)

    label = _validate_string(fields["launchAgentLabel"], "launchAgentLabel")
    if label != LAUNCH_AGENT_LABEL:
        raise ValueError("launchAgentLabel 固定为 %s，禁止注入其他 label" % LAUNCH_AGENT_LABEL)

    port = _validate_port(fields["port"])
    python_path = _validate_python_path(fields["pythonPath"])
    db_path = _validate_optional_file(fields["dbPath"], "dbPath", root)
    catalog_path = _validate_optional_file(fields["catalogPath"], "catalogPath", root)
    if db_path is None and catalog_path is None:
        raise ValueError("dbPath 与 catalogPath 至少需要一个（可以为 null 但不能同时为 null）")
    board_script = _validate_board_script(fields["boardScript"], root, expected_sha)
    runtime_dir = _validate_runtime_dir(fields["runtimeDir"], root)

    return AppConfig(
        schema_version=schema_version,
        host=host,
        port=port,
        db_path=db_path,
        catalog_path=catalog_path,
        python_path=python_path,
        board_script=board_script,
        runtime_dir=runtime_dir,
        label=label,
    )


def build_board_argv(validated: AppConfig) -> Tuple[str, ...]:
    """生成精确 argv 元组；只接受 ``AppConfig``，不接受 dict。

    顺序固定为：解释器、``-B``、board 脚本、sources（``--db``/``--catalog``，存在才带）、
    ``--runtime-dir <runtimeDir>``、``--port <port>``（末位）。``--runtime-dir`` 让被启动的
    board 以固定配置启用 ``/healthz``；本函数只返回元组，不启动任何进程或服务。
    """
    _require_app_config(validated, "build_board_argv")
    argv = [validated.python_path, "-B", validated.board_script]
    if validated.db_path is not None:
        argv.extend(["--db", validated.db_path])
    if validated.catalog_path is not None:
        argv.extend(["--catalog", validated.catalog_path])
    argv.extend(["--runtime-dir", validated.runtime_dir, "--port", str(validated.port)])
    return tuple(argv)


def build_launchd_plist(validated: AppConfig) -> bytes:
    """生成 launchd plist 字节；只接受 ``AppConfig``，只返回字节，不写文件。"""
    _require_app_config(validated, "build_launchd_plist")
    runtime_dir = validated.runtime_dir
    _require_directory(runtime_dir, "runtimeDir")
    logs_dir = runtime_dir + "/" + LOG_DIR_NAME
    _require_directory(logs_dir, "runtimeDir/" + LOG_DIR_NAME)
    stdout_path = _validate_log_target(logs_dir, STDOUT_LOG_NAME)
    stderr_path = _validate_log_target(logs_dir, STDERR_LOG_NAME)

    payload = {
        "Label": LAUNCH_AGENT_LABEL,
        "ProgramArguments": list(build_board_argv(validated)),
        "WorkingDirectory": runtime_dir,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": THROTTLE_INTERVAL,
        "StandardOutPath": stdout_path,
        "StandardErrorPath": stderr_path,
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
        "ProcessType": PROCESS_TYPE,
    }
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML)


def _require_app_config(validated: Any, builder: str) -> AppConfig:
    if not isinstance(validated, AppConfig):
        raise TypeError("%s 只接受 AppConfig，收到 %s" % (builder, type(validated).__name__))
    return validated


def _validate_field_set(config: Any) -> dict:
    if isinstance(config, (str, bytes, bytearray)) or not isinstance(config, Mapping):
        raise ValueError("config 必须是包含九个固定字段的映射")
    keys = set(config.keys())
    missing = [name for name in CONFIG_FIELDS if name not in keys]
    extra = sorted((name for name in keys if name not in CONFIG_FIELDS), key=repr)
    if missing or extra:
        problems = []
        if missing:
            problems.append("缺少字段 " + ", ".join(missing))
        if extra:
            problems.append("存在额外字段 " + ", ".join(repr(name) for name in extra))
        raise ValueError("config 字段不匹配：" + "；".join(problems))
    return {name: config[name] for name in CONFIG_FIELDS}


def _validate_string(value: Any, field: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("%s 必须是字符串" % field)
    if value == "":
        raise ValueError("%s 不能是空字符串" % field)
    for char in value:
        if char == "\x00" or unicodedata.category(char).startswith("C"):
            raise ValueError("%s 不能包含 NUL、控制字符或非法 Unicode 字符" % field)
    return value


def _validate_canonical_path(value: Any, field: str) -> str:
    text = _validate_string(value, field)
    if not text.startswith("/"):
        raise ValueError("%s 必须是绝对路径" % field)
    for part in text.split("/")[1:]:
        if part in ("", ".", ".."):
            raise ValueError("%s 不能包含空段、'.' 或 '..'（不做 resolve 兜底）" % field)
    return text


def _validate_not_temporary(path: str, field: str) -> str:
    for prefix in TEMPORARY_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            raise ValueError("%s 不允许指向临时目录 %s" % (field, prefix))
    return path


def _validate_inside_root(path: str, root: str, field: str) -> str:
    if path != root and not path.startswith(root + "/"):
        raise ValueError("%s 必须位于 allowed_root 之内" % field)
    return path


def _reject_symlink_component(root: str, path: str, field: str) -> str:
    if path == root:
        if os.path.islink(path):
            raise ValueError("%s 不能是符号链接" % field)
        return path
    current = root
    for part in path[len(root) + 1:].split("/"):
        current = current + "/" + part
        if os.path.islink(current):
            raise ValueError("%s 的路径任一层都不能是符号链接" % field)
    return path


def _reject_symlink_ancestors(path: str, field: str) -> str:
    """拒绝路径任意一层（最末层与全部祖先层）为已存在符号链接。

    纯词法逐层检查 ``os.path.islink``，不做 ``resolve``/``realpath`` 归一化，
    因此不会把用户经 symlink 绕入的路径悄悄接受为“已归一化的合法路径”。
    """
    current = ""
    for part in path.split("/")[1:]:
        current = current + "/" + part
        if os.path.islink(current):
            raise ValueError("%s 的路径任一层（含祖先层）都不能是符号链接" % field)
    return path


def _require_regular_file(path: str, field: str) -> str:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ValueError("%s 必须是已存在的普通文件" % field) from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("%s 必须是已存在的普通文件（不接受符号链接或目录）" % field)
    return path


def _require_directory(path: str, field: str) -> str:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise ValueError("%s 必须是已存在的目录" % field) from exc
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("%s 必须是已存在的目录（不接受符号链接或普通文件）" % field)
    return path


def _sha256_of_regular_file(path: str, field: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("%s 无法以非符号链接方式打开" % field) from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValueError("%s 必须是普通文件" % field)
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, _READ_CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _validate_allowed_root(value: Any) -> str:
    root = _validate_canonical_path(value, "allowed_root")
    _validate_not_temporary(root, "allowed_root")
    _reject_symlink_ancestors(root, "allowed_root")
    return _require_directory(root, "allowed_root")


def _validate_expected_sha(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, str):
        raise ValueError("expected_board_sha256 必须是小写十六进制字符串")
    if len(value) != 64 or any(char not in _LOWER_HEX for char in value):
        raise ValueError("expected_board_sha256 必须是 64 位小写十六进制 sha256")
    return value


def _validate_port(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("port 必须是整数且不能是 bool")
    if value < PORT_MIN or value > PORT_MAX:
        raise ValueError("port 必须在 %d..%d 之间" % (PORT_MIN, PORT_MAX))
    return value


def _validate_python_path(value: Any) -> str:
    text = _validate_string(value, "pythonPath")
    if text != ALLOWED_PYTHON_PATH:
        raise ValueError("pythonPath 只能是 %s" % ALLOWED_PYTHON_PATH)
    try:
        info = os.stat(text)
    except OSError as exc:
        raise ValueError("pythonPath 必须是存在的普通可执行文件") from exc
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("pythonPath 必须是普通文件")
    if not os.access(text, os.X_OK):
        raise ValueError("pythonPath 必须可执行")
    return text


def _validate_optional_file(value: Any, field: str, root: str) -> Optional[str]:
    if value is None:
        return None
    path = _validate_canonical_path(value, field)
    _validate_not_temporary(path, field)
    _validate_inside_root(path, root, field)
    _reject_symlink_component(root, path, field)
    return _require_regular_file(path, field)


def _validate_board_script(value: Any, root: str, expected_sha: str) -> str:
    field = "boardScript"
    path = _validate_canonical_path(value, field)
    _validate_not_temporary(path, field)
    _validate_inside_root(path, root, field)
    if path.rsplit("/", 1)[-1] != BOARD_SCRIPT_BASENAME:
        raise ValueError("%s 的文件名必须是 %s" % (field, BOARD_SCRIPT_BASENAME))
    _reject_symlink_component(root, path, field)
    _require_regular_file(path, field)
    if _sha256_of_regular_file(path, field) != expected_sha:
        raise ValueError("%s 的内容 sha256 与受信参数 expected_board_sha256 不一致" % field)
    return path


def _validate_log_target(logs_dir: str, name: str) -> str:
    target = logs_dir + "/" + name
    field = "runtimeDir/%s/%s" % (LOG_DIR_NAME, name)
    if os.path.islink(target):
        raise ValueError("%s 不能是符号链接" % field)
    if os.path.lexists(target) and not stat.S_ISREG(os.lstat(target).st_mode):
        raise ValueError("%s 已存在时必须是普通文件" % field)
    return target


def _validate_runtime_dir(value: Any, root: str) -> str:
    field = "runtimeDir"
    path = _validate_canonical_path(value, field)
    _validate_not_temporary(path, field)
    _validate_inside_root(path, root, field)
    _reject_symlink_component(root, path, field)
    _require_directory(path, field)
    logs_dir = path + "/" + LOG_DIR_NAME
    _reject_symlink_component(root, logs_dir, "runtimeDir/" + LOG_DIR_NAME)
    _require_directory(logs_dir, "runtimeDir/" + LOG_DIR_NAME)
    for name in (STDOUT_LOG_NAME, STDERR_LOG_NAME):
        target = logs_dir + "/" + name
        _reject_symlink_component(root, target, "runtimeDir/%s/%s" % (LOG_DIR_NAME, name))
        _validate_log_target(logs_dir, name)
    return path
