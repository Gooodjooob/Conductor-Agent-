"""D4 看板离线数据备份 / 恢复候选（G2-E，只读写自有 fixture；不安装、不启动、不联网）。

职责（只有这三个显式函数）
--------------------------
1. ``backup_snapshot(config_mapping, *, allowed_root, expected_board_sha256,
   artifact_root, output_name)``
   在调用方受信 ``artifact_root`` 之下**新建**一个输出目录，把 G1 配置指向的数据来源
   做一份离线、只读快照：

   * ``database/board.sqlite3``：用 SQLite 在线备份 API 从**只读**源连接复制出的新库；
   * ``catalog/catalog.json``：配置里 catalog 的按字节拷贝；
   * ``catalog/<snapshot>``：catalog 引用的每个材料快照（保持相对结构，便于整体搬迁）；
   * ``config.json``：本次校验过的九字段规范 JSON 字节（记录“这是哪份配置的快照”）；
   * ``manifest.json``：清单（每个被拷贝文件的相对路径 / sha256 / 字节数 / 权限、
     来源身份、拷贝文件计数，以及 ``SYNTHETIC_FIXTURE_ONLY`` 证据标记）。

2. ``verify_snapshot(snapshot_root)``
   只读校验一份快照：完全按 ``manifest.json`` 里列出的**精确路径**（限定在快照根之内）
   逐文件核对，拒绝缺失 / 被改 / 多余 / 符号链接文件与不完整 / 畸形清单；核对 SQLite 是否
   满足完整 18 表契约（``REQUIRED_SCHEMA``）、``integrity_check`` 是否为 ``ok``，以及 catalog
   引用的材料拷贝是否与声明的 sha256 一致。**只读**，绝不改动快照。

3. ``clone_snapshot(snapshot_root, *, artifact_root, output_name)``
   从一份已通过校验的快照在 ``artifact_root`` 下**新建**一份克隆，绝不覆盖活动库 / 运行时 /
   服务；克隆后核对恢复库的 ``integrity_check`` 与“含义等价”（schema + 行数 + 行内容摘要），
   **不假设**物理字节相同。

硬边界（fail-closed）
--------------------
* 只做「读取来源 + 在受信根下新建输出」。不安装、不加载、不启停任何服务或进程，不写运行时
  目录 / 日志 / 业务库，不发起网络请求，不执行 Git / 同步 / 发布，不读任何凭据 / token / 会话。
* 来源库一律以 ``mode=ro`` URI + ``PRAGMA query_only = ON`` 打开；WAL 头一律在连接**之前**
  拒绝（只读打开可能创建/恢复 ``-wal``/``-shm`` 边车），与冻结运行时的判定保持一致；**绝不**
  对源库做 checkpoint / 修复 / 迁移 / 就地修改，也**绝不**使用 ``immutable=1`` 去忽略活 WAL。
* SQLite 备份用在线 backup API 写**新**文件；备份带有限、确定性的进度预算与页预算，超限即中止
  并只删除**本次新建**的输出目录。
* 备份前后在**同一条只读连接**上比较 ``PRAGMA data_version``，并对源文件比较 ``(device, inode)``
  身份；catalog / 材料在备份前后各算一次摘要；检测到任何可探测漂移即 fail-closed。
  注意：**没有**任何事务能同时覆盖 SQLite 与 catalog 文件系统，两者只是各自前后各测一次，
  只能“拒绝可探测的漂移”，不能声称二者有任何跨介质原子性。
* catalog 材料一律走冻结运行时的描述符相对遍历原语（``_material_bytes``：逐段
  ``O_DIRECTORY``/``O_NOFOLLOW``，末段 ``O_RDONLY``/``O_NOFOLLOW``），任一层符号链接都被拒绝；
  材料的 ``sourcePath`` 字段**从不**被打开。
* 输出权限固定 0700（目录）/ 0600（文件）；输出目录必须位于受信 ``artifact_root`` 之内、是单个
  安全路径段、且**不存在**（不覆盖任何已有文件 / 目录 / 符号链接）；临时目录与安装位置一律拒绝。
* 没有默认查找：来源只来自调用方显式传入的九字段配置，且必须落在显式 ``allowed_root`` 之内。
* 不生成整库内容哈希：来源库只记录 ``(pathSha256, device, inode)`` 身份，绝不整库哈希。

诚实边界（本卡未实现、未验证）
-----------------------------
* 真实 macOS 登录启动 / 崩溃恢复 / 双击 / launch agent 加载 / 重启 / 日常迁移全部 **NOT_RUN**：
  本模块只提供“离线、只读”的来源侧工具，任何真实服务动作都需要后续单独授权。
* 不是完整 MVP：本模块只负责“把这些来源离线快照成一份新产物、并只读校验 / 克隆”，不含安装、
  服务注册、迁移编排、发布或稳定版资格判定。
* 本轮 worker 的 Bash 被禁用：**测试与 SHA 全部 NOT_RUN**，真实运行与独立核验属于 cox。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import sys
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

__all__ = ["RecoveryError", "backup_snapshot", "clone_snapshot", "verify_snapshot"]


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
MANIFEST_SCHEMA = "d4-app-recovery-manifest.v1"
VERIFY_SCHEMA = "d4-app-recovery-verify.v1"
EVIDENCE_MARKER = "SYNTHETIC_FIXTURE_ONLY"

MANIFEST_NAME = "manifest.json"
CONFIG_NAME = "config.json"
DATABASE_DIR = "database"
DATABASE_NAME = "board.sqlite3"
CATALOG_DIR = "catalog"
CATALOG_NAME = "catalog.json"

ROLE_DATABASE = "database"
ROLE_CATALOG = "catalog"
ROLE_MATERIAL = "material"
ROLE_CONFIG = "config"

MODE_DIR = 0o700
MODE_FILE = 0o600

MAX_DESTINATION_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 16 * 1024
MAX_SNAPSHOT_FILES = 4096
MAX_STATE_ROWS_PER_TABLE = 5000

SQLITE_READ_ONLY_SUFFIX = "?mode=ro"
SQLITE_CONNECT_TIMEOUT_SECONDS = 0.5

# 有限、确定性的备份预算：进度回调步数与单调时钟截止时间（两者任一超限即中止）。
BACKUP_PAGES_PER_STEP = 64
MAX_BACKUP_PROGRESS_STEPS = 200_000
BACKUP_DEADLINE_SECONDS = 30.0
BACKUP_SLEEP_SECONDS = 0.01

READ_CHUNK = 65536

# 输出位置禁区：临时根目录与各类安装位置（用户级 / 系统级 Applications、LaunchAgents）。
FORBIDDEN_SEGMENT_NAMES = frozenset({"LaunchAgents"})
FORBIDDEN_PATH_PATTERNS = (
    "~/Applications",
    "~/Library/LaunchAgents",
    "/Applications",
    "/Library/LaunchAgents",
)

RECOVERY_ERROR_CODES = (
    "invalid_arguments",
    "config_invalid",
    "source_not_ready",
    "source_unreadable",
    "source_drift",
    "artifact_root_invalid",
    "output_invalid",
    "output_exists",
    "backup_failed",
    "snapshot_invalid",
    "clone_failed",
)

VERIFY_REASONS = (
    "ok",
    "snapshot_missing",
    "snapshot_symlink",
    "manifest_missing",
    "manifest_malformed",
    "manifest_incomplete",
    "file_missing",
    "file_changed",
    "file_extra",
    "file_symlink",
    "database_invalid",
    "catalog_invalid",
)

MANIFEST_REQUIRED_FIELDS = (
    "schemaVersion",
    "operation",
    "evidenceMarker",
    "outputName",
    "outputRoot",
    "boardSha256",
    "configIdentity",
    "dataSourceIdentity",
    "configSha256",
    "sources",
    "copiedFileCount",
    "copiedFileBytes",
    "files",
)

FILE_ENTRY_FIELDS = ("path", "role", "sha256", "bytes", "mode")

_LOWER_HEX = frozenset("0123456789abcdef")


# --------------------------------------------------------------------------- #
# Public error types
# --------------------------------------------------------------------------- #
class RecoveryError(Exception):
    """备份 / 克隆失败时抛出的通用错误，只携带一个固定错误码。

    ``str(error)`` 就是错误码本身：路径、SQL、异常正文与来源内容都不会进入调用方或报告。
    """

    def __init__(self, code: str) -> None:
        if code not in RECOVERY_ERROR_CODES:
            raise ValueError("invalid recovery error code")
        self.code = code
        super().__init__(code)


class _VerifyFailure(Exception):
    """内部标记：只读校验失败，携带一个 ``VERIFY_REASONS`` 里的固定原因码（不外泄）。"""

    def __init__(self, reason: str) -> None:
        if reason not in VERIFY_REASONS:
            raise ValueError("invalid verify reason")
        self.reason = reason
        super().__init__(reason)


# --------------------------------------------------------------------------- #
# Scalar / path helpers
# --------------------------------------------------------------------------- #
def _canonical_bytes(obj: Any) -> bytes:
    """规范字节：紧凑、键排序、UTF-8、无尾随换行（与冻结协议同规则）。"""
    return json.dumps(
        obj,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _is_lower_hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in _LOWER_HEX for char in value)


def _lstat(path: str) -> Optional[os.stat_result]:
    try:
        return os.lstat(path)
    except OSError:
        return None


def _string_value(value: Any, code: str) -> str:
    if isinstance(value, bool) or not isinstance(value, str) or value == "":
        raise RecoveryError(code)
    for char in value:
        if char == "\x00" or unicodedata.category(char).startswith("C"):
            raise RecoveryError(code)
    return value


def _absolute_canonical(value: Any, code: str) -> str:
    text = _string_value(value, code)
    if not text.startswith("/"):
        raise RecoveryError(code)
    for part in text.split("/")[1:]:
        if part in ("", ".", ".."):
            raise RecoveryError(code)
    return text


def _reject_temporary(path: str, code: str) -> str:
    for prefix in _APP_CONFIG.TEMPORARY_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            raise RecoveryError(code)
    return path


def _reject_forbidden_target(path: str, code: str) -> str:
    home = os.path.expanduser("~")
    for pattern in FORBIDDEN_PATH_PATTERNS:
        prefix = home + pattern[1:] if pattern.startswith("~") else pattern
        if prefix and (path == prefix or path.startswith(prefix + "/")):
            raise RecoveryError(code)
    for part in path.split("/")[1:]:
        if part in FORBIDDEN_SEGMENT_NAMES:
            raise RecoveryError(code)
    return path


def _reject_symlink_components(path: str, code: str) -> str:
    """词法逐层检查整条路径（含祖先层）是否有已存在的符号链接，不做 resolve 归一化。"""
    current = ""
    for part in path.split("/")[1:]:
        current = current + "/" + part
        if os.path.islink(current):
            raise RecoveryError(code)
    return path


def _require_directory(path: str, code: str) -> str:
    info = _lstat(path)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise RecoveryError(code)
    return path


def _validate_artifact_root(value: Any) -> str:
    path = _absolute_canonical(value, "artifact_root_invalid")
    _reject_temporary(path, "artifact_root_invalid")
    _reject_forbidden_target(path, "artifact_root_invalid")
    _reject_symlink_components(path, "artifact_root_invalid")
    return _require_directory(path, "artifact_root_invalid")


def _validate_output_name(value: Any) -> str:
    text = _string_value(value, "output_invalid")
    if text in (".", "..") or "/" in text or text.startswith("."):
        raise RecoveryError("output_invalid")
    return text


def _safe_relative_path(value: Any) -> bool:
    """True 表示一个可安全拼到快照根之下的相对路径（无绝对、无 ``.``/``..``/空段、无控制字符）。"""
    if not isinstance(value, str) or value == "" or value.startswith("/"):
        return False
    for char in value:
        if char == "\x00" or unicodedata.category(char).startswith("C"):
            return False
    for part in value.split("/"):
        if part in ("", ".", ".."):
            return False
    return True


def _make_directories(base: str, directory: str) -> None:
    relative = directory[len(base) + 1:]
    current = base
    for part in relative.split("/"):
        current = current + "/" + part
        if not os.path.isdir(current):
            os.mkdir(current, MODE_DIR)
        os.chmod(current, MODE_DIR)


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
# Bounded, read-only measurement primitives
# --------------------------------------------------------------------------- #
def _hash_file_bounded(path: str, limit: int) -> Tuple[int, str]:
    """流式读取一个已存在的普通文件并返回 (字节数, sha256)；符号链接 / 超限一律失败。"""
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise _VerifyFailure("file_missing")
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        raise _VerifyFailure("file_missing") from None
    digest = hashlib.sha256()
    total = 0
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise _VerifyFailure("file_changed")
        while True:
            chunk = os.read(descriptor, READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise _VerifyFailure("file_changed")
            digest.update(chunk)
    except OSError:
        raise _VerifyFailure("file_changed") from None
    finally:
        os.close(descriptor)
    return total, digest.hexdigest()


def _copy_new_file(source: str, target: str, mode: int, limit: int) -> Tuple[int, str]:
    """把一个只读来源文件流式拷到**新建**目标；返回 (字节数, sha256)。"""
    info = _lstat(source)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise RecoveryError("snapshot_invalid")
    read_flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        reader = os.open(source, read_flags)
    except OSError:
        raise RecoveryError("snapshot_invalid") from None
    try:
        writer = os.open(target, write_flags, mode)
    except OSError:
        os.close(reader)
        raise RecoveryError("clone_failed") from None
    digest = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(reader, READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise RecoveryError("clone_failed")
            digest.update(chunk)
            view = memoryview(chunk)
            while len(view) > 0:
                written = os.write(writer, view)
                if written <= 0:
                    raise RecoveryError("clone_failed")
                view = view[written:]
    except OSError:
        raise RecoveryError("clone_failed") from None
    finally:
        os.close(writer)
        os.close(reader)
    os.chmod(target, mode)
    return total, digest.hexdigest()


# --------------------------------------------------------------------------- #
# SQLite primitives (read-only source, bounded)
# --------------------------------------------------------------------------- #
def _open_readonly_db(path: str) -> sqlite3.Connection:
    """以 ``mode=ro`` + ``query_only`` 打开一个已存在的 SQLite 来源（WAL 头先拒绝）。"""
    if _HEALTH_RUNTIME._is_wal_header(path):
        raise RecoveryError("source_unreadable")
    uri = Path(os.path.abspath(path)).as_uri() + SQLITE_READ_ONLY_SUFFIX
    try:
        connection = sqlite3.connect(
            uri,
            uri=True,
            isolation_level=None,
            timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        )
    except sqlite3.Error:
        raise RecoveryError("source_unreadable") from None
    try:
        _HEALTH_RUNTIME._attach_step_budget(connection)
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        connection.close()
        raise RecoveryError("source_unreadable") from None
    return connection


def _data_version(connection: sqlite3.Connection) -> Optional[int]:
    try:
        row = connection.execute("PRAGMA data_version").fetchone()
    except sqlite3.Error:
        return None
    if not row or row[0] is None:
        return None
    return int(row[0])


def _integrity_ok(connection: sqlite3.Connection) -> bool:
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error:
        return False
    return len(rows) == 1 and rows[0][0] == "ok"


def _database_report(path: str) -> Dict[str, Any]:
    """只读结构探测：完整 18 表契约 + ``integrity_check`` + 有界项目 id 扫描。"""
    report = {
        "present": False,
        "readable": False,
        "schemaVerified": False,
        "integrityOk": False,
        "projectIds": 0,
        "scanComplete": False,
    }
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        return report
    report["present"] = True
    if _HEALTH_RUNTIME._is_wal_header(path):
        return report
    try:
        connection = _open_readonly_db(path)
    except RecoveryError:
        return report
    try:
        tables = _HEALTH_RUNTIME._verify_schema(connection)
        if tables is None:
            report["readable"] = True
            return report
        report["readable"] = True
        report["schemaVerified"] = True
        if not _integrity_ok(connection):
            return report
        report["integrityOk"] = True
        project_ids, complete = _HEALTH_RUNTIME._project_ids(connection, tables)
        if not complete:
            return report
        report["scanComplete"] = True
        report["projectIds"] = len(project_ids)
        return report
    except sqlite3.Error:
        return report
    finally:
        connection.close()


def _database_state(path: str) -> Optional[Dict[str, Any]]:
    """恢复库的“含义状态”：每张契约表的列名 + 行数 + 有界行内容摘要。

    用于证明克隆库与原快照库在 schema / 行数 / 行内容层面等价，而**不**假设物理字节相同。
    """
    try:
        connection = _open_readonly_db(path)
    except RecoveryError:
        return None
    try:
        tables = _HEALTH_RUNTIME._verify_schema(connection)
        if tables is None:
            return None
        state: Dict[str, Any] = {}
        for table in sorted(_HEALTH_RUNTIME.REQUIRED_SCHEMA):
            if table not in tables:
                continue
            columns = sorted(
                row[1] for row in connection.execute("PRAGMA table_info(%s)" % table) if len(row) > 1
            )
            count = connection.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0]
            rows = connection.execute(
                "SELECT * FROM %s ORDER BY rowid LIMIT %d" % (table, MAX_STATE_ROWS_PER_TABLE)
            ).fetchall()
            payload = [list(row) for row in rows]
            state[table] = {
                "columns": columns,
                "rows": int(count),
                "digest": hashlib.sha256(_canonical_bytes(payload)).hexdigest(),
            }
        return state
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def _backup_progress_limits() -> Tuple[int, float]:
    """本次备份的 (进度步数上限, 截止时间)；配置异常时回退到安全默认。"""
    steps = MAX_BACKUP_PROGRESS_STEPS
    if isinstance(steps, bool) or not isinstance(steps, int) or steps <= 0:
        steps = 200_000
    deadline = BACKUP_DEADLINE_SECONDS
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or deadline <= 0:
        deadline = 30.0
    return steps, float(deadline)


def _run_backup(source: sqlite3.Connection, destination_path: str) -> None:
    """用在线备份 API 把只读源连接复制到**新建**目标库，带有限进度/页/时间预算。"""
    max_steps, deadline_seconds = _backup_progress_limits()
    deadline = time.monotonic() + deadline_seconds
    counter = {"steps": 0}
    page_size = source.execute("PRAGMA page_size").fetchone()[0]

    def _progress(_status: int, _remaining: int, _total: int) -> None:
        counter["steps"] += 1
        if _total * page_size > MAX_DESTINATION_BYTES:
            raise RecoveryError("backup_failed")
        if counter["steps"] > max_steps:
            raise RecoveryError("backup_failed")
        if time.monotonic() > deadline:
            raise RecoveryError("backup_failed")
        return None

    destination = sqlite3.connect(destination_path, isolation_level=None)
    try:
        source.backup(
            destination,
            pages=BACKUP_PAGES_PER_STEP,
            progress=_progress,
            sleep=BACKUP_SLEEP_SECONDS,
        )
    except RecoveryError:
        raise
    except sqlite3.Error:
        raise RecoveryError("backup_failed") from None
    finally:
        try:
            destination.close()
        except sqlite3.Error:  # pragma: no cover - defensive
            pass


def _discard_sidecars(database_path: str) -> None:
    """删除**本次新建**备份库旁边由备份过程产生的临时边车（仅限我们自己的新输出）。"""
    for suffix in ("-journal", "-wal", "-shm"):
        sidecar = database_path + suffix
        if os.path.lexists(sidecar):
            try:
                os.unlink(sidecar)
            except OSError:  # pragma: no cover - defensive
                pass


def _db_file_identity(path: str) -> Optional[Dict[str, Any]]:
    info = _lstat(path)
    if info is None or not stat.S_ISREG(info.st_mode):
        return None
    return {
        "pathSha256": hashlib.sha256(path.encode("utf-8")).hexdigest(),
        "device": int(info.st_dev),
        "inode": int(info.st_ino),
    }


# --------------------------------------------------------------------------- #
# Catalog primitives (confined descriptor traversal, reuse of frozen runtime)
# --------------------------------------------------------------------------- #
def _iter_catalog_materials(data: Any):
    """按冻结 catalog 结构产出 (snapshot, declared_sha256)，只读且不碰 ``sourcePath``。"""
    rows = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return
    for project in rows:
        if not isinstance(project, dict):
            continue
        tasks = project.get("tasks")
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            documents = task.get("documents")
            if not isinstance(documents, list):
                continue
            for document in documents:
                if not isinstance(document, dict):
                    continue
                snapshot = document.get("snapshot")
                declared = document.get("sha256")
                if _HEALTH_RUNTIME._safe_catalog_material(snapshot) and _is_lower_hex64(declared):
                    yield snapshot, declared


def _read_catalog_state(catalog_path: str) -> Dict[str, Any]:
    """只读读取并结构化校验 catalog，返回 (字节, 摘要, 材料清单)；失败即 ``source_not_ready``。"""
    parent = os.path.dirname(catalog_path)
    try:
        raw = _HEALTH_RUNTIME._bounded_read(catalog_path, _HEALTH_RUNTIME.MAX_CATALOG_BYTES)
    except (_HEALTH_RUNTIME._ReadFailed, _HEALTH_RUNTIME._ReadBoundExceeded):
        raise RecoveryError("source_not_ready") from None
    digest = hashlib.sha256(raw).hexdigest()
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise RecoveryError("source_not_ready") from None
    if _HEALTH_RUNTIME._validate_catalog(data, parent) is None:
        raise RecoveryError("source_not_ready")
    materials: List[Tuple[str, str]] = []
    total = 0
    for snapshot, declared in _iter_catalog_materials(data):
        if len(materials) >= _HEALTH_RUNTIME.MAX_CATALOG_MATERIALS:
            raise RecoveryError("source_not_ready")
        material = _HEALTH_RUNTIME._material_bytes(parent, snapshot)
        if material is None:
            raise RecoveryError("source_not_ready")
        total += len(material)
        if total > _HEALTH_RUNTIME.MAX_CATALOG_TOTAL_MATERIAL_BYTES:
            raise RecoveryError("source_not_ready")
        if hashlib.sha256(material).hexdigest() != declared:
            raise RecoveryError("source_not_ready")
        materials.append((snapshot, declared))
    return {"bytes": raw, "digest": digest, "materials": tuple(materials), "parent": parent}


def _catalog_fingerprint(state: Optional[Dict[str, Any]]) -> Any:
    if state is None:
        return None
    return (state["digest"], tuple(state["materials"]))


# --------------------------------------------------------------------------- #
# Step 1: G1 校验 + freeze-readiness（全部在创建输出之前完成）
# --------------------------------------------------------------------------- #
def _validate_source(
    config_mapping: Any, allowed_root: Any, expected_board_sha256: Any
) -> Tuple[Any, Any]:
    """用 G1 校验配置/root/board SHA，再用冻结运行时确认来源就绪。"""
    try:
        validated = _APP_CONFIG.validate_config(
            config_mapping,
            allowed_root=allowed_root,
            expected_board_sha256=expected_board_sha256,
        )
    except (ValueError, TypeError, OSError):
        raise RecoveryError("config_invalid") from None

    for source in (validated.db_path, validated.catalog_path):
        if source is not None:
            _reject_symlink_components(source, "config_invalid")

    try:
        frozen = _HEALTH_RUNTIME.freeze_identity(
            validated, expected_board_sha256=expected_board_sha256
        )
    except _HEALTH_RUNTIME.HealthRuntimeError:
        raise RecoveryError("source_not_ready") from None

    try:
        payload = _HEALTH_RUNTIME.collect_health(frozen)
    except _HEALTH_RUNTIME.HealthRuntimeError:
        raise RecoveryError("source_not_ready") from None

    if payload.get("ready") is not True:
        raise RecoveryError("source_not_ready")
    return validated, frozen


def _serialize_config(validated: Any) -> bytes:
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
        raise RecoveryError("config_invalid") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise RecoveryError("config_invalid")
    return raw


# --------------------------------------------------------------------------- #
# Step 2: 来源快照（数据库 / catalog）
# --------------------------------------------------------------------------- #
def _copy_database(source_path: str, output_dir: str) -> Dict[str, Any]:
    """把只读源库备份成输出目录里的新库，并在同一条只读连接上核对漂移。"""
    if _HEALTH_RUNTIME._is_wal_header(source_path):
        raise RecoveryError("source_unreadable")

    info = _lstat(source_path)
    if info is None or info.st_size > MAX_DESTINATION_BYTES:
        raise RecoveryError("backup_failed")
    identity_before = _db_file_identity(source_path)
    if identity_before is None:
        raise RecoveryError("source_not_ready")

    relative = DATABASE_DIR + "/" + DATABASE_NAME
    destination_path = output_dir + "/" + relative
    _make_directories(output_dir, os.path.dirname(destination_path))

    source = _open_readonly_db(source_path)
    try:
        version_before = _data_version(source)
        _run_backup(source, destination_path)
        version_after = _data_version(source)
    finally:
        source.close()

    _discard_sidecars(destination_path)
    os.chmod(destination_path, MODE_FILE)

    identity_after = _db_file_identity(source_path)
    if identity_after != identity_before:
        raise RecoveryError("source_drift")
    if version_before is None or version_after is None or version_before != version_after:
        raise RecoveryError("source_drift")

    report = _database_report(destination_path)
    if not (report["present"] and report["schemaVerified"] and report["integrityOk"] and report["scanComplete"]):
        raise RecoveryError("backup_failed")

    total, digest = _hash_file_bounded(destination_path, MAX_DESTINATION_BYTES)
    return {
        "role": ROLE_DATABASE,
        "path": relative,
        "sha256": digest,
        "bytes": total,
        "mode": "0%03o" % MODE_FILE,
    }


def _write_catalog(output_dir: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """按字节写出 catalog 与全部被引用材料（保持相对结构，便于整体搬迁）。"""
    records: List[Dict[str, Any]] = []
    if any(snapshot == CATALOG_NAME for snapshot, _declared in state["materials"]):
        # catalog 自身的文件名不能同时被某个材料占用。
        raise RecoveryError("backup_failed")

    catalog_relative = CATALOG_DIR + "/" + CATALOG_NAME
    _make_directories(output_dir, output_dir + "/" + CATALOG_DIR)
    _write_new_file(output_dir + "/" + catalog_relative, state["bytes"], MODE_FILE)
    records.append({
        "role": ROLE_CATALOG,
        "path": catalog_relative,
        "sha256": hashlib.sha256(state["bytes"]).hexdigest(),
        "bytes": len(state["bytes"]),
        "mode": "0%03o" % MODE_FILE,
    })

    total = 0
    for snapshot, declared in state["materials"]:
        material = _HEALTH_RUNTIME._material_bytes(state["parent"], snapshot)
        if material is None:
            raise RecoveryError("source_drift")
        total += len(material)
        if total > _HEALTH_RUNTIME.MAX_CATALOG_TOTAL_MATERIAL_BYTES:
            raise RecoveryError("source_drift")
        digest = hashlib.sha256(material).hexdigest()
        if digest != declared:
            raise RecoveryError("source_drift")
        relative = CATALOG_DIR + "/" + snapshot
        target = output_dir + "/" + relative
        _make_directories(output_dir, os.path.dirname(target))
        _write_new_file(target, material, MODE_FILE)
        records.append({
            "role": ROLE_MATERIAL,
            "path": relative,
            "sha256": digest,
            "bytes": len(material),
            "mode": "0%03o" % MODE_FILE,
        })
    return records


def _reject_overlapping_output(output_dir: str, sources: List[Optional[str]]) -> None:
    for source in sources:
        if source is None:
            continue
        if source == output_dir or source.startswith(output_dir + "/"):
            raise RecoveryError("output_invalid")


def _source_record(frozen: Any, slot: str) -> Dict[str, Any]:
    record = frozen.sources[slot]
    if record is None:
        return {"present": False, "metadata": None, "contentSha256": None}
    metadata = record.get("metadata")
    return {
        "present": True,
        "metadata": dict(metadata) if metadata is not None else None,
        "contentSha256": record.get("contentSha256"),
    }


# --------------------------------------------------------------------------- #
# Public: backup_snapshot
# --------------------------------------------------------------------------- #
def backup_snapshot(
    config_mapping: Any,
    *,
    allowed_root: Any,
    expected_board_sha256: Any,
    artifact_root: Any,
    output_name: Any,
) -> Dict[str, Any]:
    """在受信 ``artifact_root`` 下新建 ``output_name`` 目录，写出一份离线只读快照。

    顺序固定且 fail-closed：先 G1 校验 + freeze-readiness（不写任何东西），再校验输出位置
    （必须不存在），随后才新建输出目录；任何失败都会删除**本次调用新建的**那个输出目录，
    绝不触碰其他既有文件、来源文件或同级邻居。

    返回生成的 manifest（``d4-app-recovery-manifest.v1``）。失败抛 :class:`RecoveryError`，
    只带一个固定错误码。
    """
    validated, frozen = _validate_source(config_mapping, allowed_root, expected_board_sha256)

    root = _validate_artifact_root(artifact_root)
    name = _validate_output_name(output_name)
    output_dir = root + "/" + name
    if os.path.lexists(output_dir):
        raise RecoveryError("output_exists")
    _reject_overlapping_output(output_dir, [validated.db_path, validated.catalog_path])

    db_path = validated.db_path
    catalog_path = validated.catalog_path

    # —— 来源漂移基准：在写任何东西之前先记一次 catalog 状态 —— #
    catalog_before = _read_catalog_state(catalog_path) if catalog_path is not None else None

    created = False
    try:
        os.mkdir(output_dir, MODE_DIR)
        created = True
        os.chmod(output_dir, MODE_DIR)

        files: List[Dict[str, Any]] = []
        if db_path is not None:
            files.append(_copy_database(db_path, output_dir))

        if catalog_path is not None:
            catalog_after = _read_catalog_state(catalog_path)
            if _catalog_fingerprint(catalog_before) != _catalog_fingerprint(catalog_after):
                raise RecoveryError("source_drift")
            files.extend(_write_catalog(output_dir, catalog_after))

        config_bytes = _serialize_config(validated)
        _write_new_file(output_dir + "/" + CONFIG_NAME, config_bytes, MODE_FILE)
        config_digest = hashlib.sha256(config_bytes).hexdigest()
        files.append({
            "role": ROLE_CONFIG,
            "path": CONFIG_NAME,
            "sha256": config_digest,
            "bytes": len(config_bytes),
            "mode": "0%03o" % MODE_FILE,
        })

        files.sort(key=lambda item: item["path"])
        manifest = {
            "schemaVersion": MANIFEST_SCHEMA,
            "operation": "backup",
            "evidenceMarker": EVIDENCE_MARKER,
            "outputName": name,
            "outputRoot": output_dir,
            "boardSha256": frozen.board_sha256,
            "configIdentity": frozen.config_identity,
            "dataSourceIdentity": frozen.data_source_identity,
            "configSha256": config_digest,
            "sources": {
                "db": _source_record(frozen, "db"),
                "catalog": _source_record(frozen, "catalog"),
            },
            "copiedFileCount": len(files),
            "copiedFileBytes": sum(item["bytes"] for item in files),
            "files": files,
        }
        _write_new_file(output_dir + "/" + MANIFEST_NAME, _manifest_bytes(manifest), MODE_FILE)
    except RecoveryError:
        if created:
            shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except BaseException:
        if created:
            shutil.rmtree(output_dir, ignore_errors=True)
        raise RecoveryError("backup_failed") from None

    return manifest


def _manifest_bytes(manifest: Dict[str, Any]) -> bytes:
    try:
        raw = _canonical_bytes(manifest)
    except (TypeError, ValueError):
        raise RecoveryError("backup_failed") from None
    if len(raw) > MAX_MANIFEST_BYTES:
        raise RecoveryError("backup_failed")
    return raw


# --------------------------------------------------------------------------- #
# Snapshot enumeration + manifest validation (read-only)
# --------------------------------------------------------------------------- #
def _iter_snapshot_files(root: str) -> List[str]:
    """有界遍历快照目录，返回相对文件路径；符号链接 / 特殊文件 / 超限一律失败。"""
    found: List[str] = []
    stack = [""]
    nodes = 0
    while stack:
        relative_dir = stack.pop()
        absolute_dir = root + ("/" + relative_dir if relative_dir else "")
        try:
            entries = []
            with os.scandir(absolute_dir) as iterator:
                for entry in iterator:
                    nodes += 1
                    if nodes > MAX_SNAPSHOT_FILES * 2 + 32:
                        raise _VerifyFailure("file_extra")
                    entries.append(entry)
            entries.sort(key=lambda entry: entry.name)
        except OSError:
            raise _VerifyFailure("snapshot_missing") from None
        for entry in entries:
            child = entry.name if not relative_dir else relative_dir + "/" + entry.name
            try:
                if entry.is_symlink():
                    raise _VerifyFailure("file_symlink")
                if entry.is_dir(follow_symlinks=False):
                    stack.append(child)
                elif entry.is_file(follow_symlinks=False):
                    found.append(child)
                    if len(found) > MAX_SNAPSHOT_FILES:
                        raise _VerifyFailure("file_extra")
                else:
                    raise _VerifyFailure("file_extra")
            except OSError:
                raise _VerifyFailure("snapshot_missing") from None
    return found


def _read_manifest(root: str) -> Tuple[Dict[str, Any], bytes, str]:
    manifest_path = root + "/" + MANIFEST_NAME
    info = _lstat(manifest_path)
    if info is None or not stat.S_ISREG(info.st_mode):
        raise _VerifyFailure("manifest_missing")
    if os.path.islink(manifest_path):
        raise _VerifyFailure("file_symlink")
    try:
        raw = _HEALTH_RUNTIME._bounded_read(manifest_path, MAX_MANIFEST_BYTES)
    except (_HEALTH_RUNTIME._ReadFailed, _HEALTH_RUNTIME._ReadBoundExceeded):
        raise _VerifyFailure("manifest_missing") from None
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise _VerifyFailure("manifest_malformed") from None
    if not isinstance(manifest, dict) or manifest.get("schemaVersion") != MANIFEST_SCHEMA:
        raise _VerifyFailure("manifest_malformed")
    return manifest, raw, hashlib.sha256(raw).hexdigest()


def _validate_manifest(manifest: Dict[str, Any]) -> List[Dict[str, Any]]:
    for field in MANIFEST_REQUIRED_FIELDS:
        if field not in manifest:
            raise _VerifyFailure("manifest_incomplete")
    files = manifest["files"]
    if not isinstance(files, list) or not files or len(files) > MAX_SNAPSHOT_FILES:
        raise _VerifyFailure("manifest_incomplete")
    entries: List[Dict[str, Any]] = []
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise _VerifyFailure("manifest_incomplete")
        for field in FILE_ENTRY_FIELDS:
            if field not in item:
                raise _VerifyFailure("manifest_incomplete")
        path = item["path"]
        if not _safe_relative_path(path) or path in seen:
            raise _VerifyFailure("manifest_incomplete")
        seen.add(path)
        if item["role"] not in (ROLE_DATABASE, ROLE_CATALOG, ROLE_MATERIAL, ROLE_CONFIG):
            raise _VerifyFailure("manifest_incomplete")
        if not _is_lower_hex64(item["sha256"]):
            raise _VerifyFailure("manifest_incomplete")
        if isinstance(item["bytes"], bool) or not isinstance(item["bytes"], int) or item["bytes"] < 0:
            raise _VerifyFailure("manifest_incomplete")
        if not isinstance(item["mode"], str):
            raise _VerifyFailure("manifest_incomplete")
        entries.append(item)
    if manifest.get("copiedFileCount") != len(entries):
        raise _VerifyFailure("manifest_incomplete")
    roles = [item["role"] for item in entries]
    if roles.count(ROLE_CONFIG) != 1 or roles.count(ROLE_DATABASE) > 1 or roles.count(ROLE_CATALOG) > 1:
        raise _VerifyFailure("manifest_incomplete")
    if ROLE_DATABASE not in roles and ROLE_CATALOG not in roles:
        raise _VerifyFailure("manifest_incomplete")
    total = sum(item["bytes"] for item in entries)
    maximum = MAX_DESTINATION_BYTES + _HEALTH_RUNTIME.MAX_CATALOG_BYTES + _HEALTH_RUNTIME.MAX_CATALOG_TOTAL_MATERIAL_BYTES + 16384
    if manifest.get("copiedFileBytes") != total or total > maximum:
        raise _VerifyFailure("manifest_incomplete")
    for field in ("boardSha256", "configIdentity", "dataSourceIdentity", "configSha256"):
        if not _is_lower_hex64(manifest[field]):
            raise _VerifyFailure("manifest_incomplete")
    return entries


def _snapshot_root(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("/") or value == "/":
        raise _VerifyFailure("snapshot_missing")
    if os.path.islink(value):
        raise _VerifyFailure("snapshot_symlink")
    info = _lstat(value)
    if info is None or not stat.S_ISDIR(info.st_mode):
        raise _VerifyFailure("snapshot_missing")
    return value


def _verify_catalog(root: str, catalog_entry: Dict[str, Any], entries: List[Dict[str, Any]]) -> bool:
    """核对 catalog 引用的材料拷贝是否与声明的 sha256（以及清单记录）一致。"""
    catalog_path = root + "/" + catalog_entry["path"]
    parent = os.path.dirname(catalog_path)
    try:
        raw = _HEALTH_RUNTIME._bounded_read(catalog_path, _HEALTH_RUNTIME.MAX_CATALOG_BYTES)
    except (_HEALTH_RUNTIME._ReadFailed, _HEALTH_RUNTIME._ReadBoundExceeded):
        return False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return False
    by_path = {item["path"]: item for item in entries}
    catalog_dir = os.path.dirname(catalog_entry["path"])
    prefix = catalog_dir + "/" if catalog_dir else ""
    count = 0
    for snapshot, declared in _iter_catalog_materials(data):
        count += 1
        if count > _HEALTH_RUNTIME.MAX_CATALOG_MATERIALS:
            return False
        relative = prefix + snapshot
        item = by_path.get(relative)
        if item is None or item["role"] != ROLE_MATERIAL or item["sha256"] != declared:
            return False
        material_path = parent + "/" + snapshot
        try:
            _size, digest = _hash_file_bounded(material_path, _HEALTH_RUNTIME.MAX_CATALOG_MATERIAL_BYTES)
        except _VerifyFailure:
            return False
        if digest != declared:
            return False
    return True


def _verify_snapshot(snapshot_root: Any) -> Dict[str, Any]:
    root = _snapshot_root(snapshot_root)
    manifest, _raw, _digest = _read_manifest(root)
    entries = _validate_manifest(manifest)

    expected = {MANIFEST_NAME}
    expected.update(item["path"] for item in entries)
    found = set(_iter_snapshot_files(root))
    if found - expected:
        raise _VerifyFailure("file_extra")

    for item in entries:
        path = root + "/" + item["path"]
        info = _lstat(path)
        if info is None:
            raise _VerifyFailure("file_missing")
        if stat.S_ISLNK(info.st_mode) or os.path.islink(path):
            raise _VerifyFailure("file_symlink")
        if not stat.S_ISREG(info.st_mode):
            raise _VerifyFailure("file_extra")
        try:
            size, digest = _hash_file_bounded(path, MAX_DESTINATION_BYTES)
        except _VerifyFailure as failure:
            raise _VerifyFailure(failure.reason)
        if digest != item["sha256"] or size != item["bytes"]:
            raise _VerifyFailure("file_changed")

    config_entry = next(item for item in entries if item["role"] == ROLE_CONFIG)
    try:
        raw_config = _HEALTH_RUNTIME._bounded_read(root + "/" + config_entry["path"], 16384)
        config = json.loads(raw_config.decode("utf-8"))
        identity = _HEALTH_RUNTIME._reader.compute_config_identity(config, board_sha256=manifest["boardSha256"])
    except (ValueError, RecursionError, _HEALTH_RUNTIME._ReadFailed, _HEALTH_RUNTIME._ReadBoundExceeded):
        raise _VerifyFailure("manifest_malformed") from None
    if identity != manifest["configIdentity"] or hashlib.sha256(raw_config).hexdigest() != manifest["configSha256"]:
        raise _VerifyFailure("manifest_incomplete")
    roles = {item["role"] for item in entries}
    if (config["dbPath"] is not None) != (ROLE_DATABASE in roles) or (config["catalogPath"] is not None) != (ROLE_CATALOG in roles):
        raise _VerifyFailure("manifest_incomplete")
    database_entry = next((item for item in entries if item["role"] == ROLE_DATABASE), None)
    catalog_entry = next((item for item in entries if item["role"] == ROLE_CATALOG), None)

    database_verified = False
    if database_entry is not None:
        report = _database_report(root + "/" + database_entry["path"])
        if not (report["present"] and report["schemaVerified"] and report["integrityOk"] and report["scanComplete"]):
            raise _VerifyFailure("database_invalid")
        database_verified = True

    catalog_verified = False
    if catalog_entry is not None:
        if not _verify_catalog(root, catalog_entry, entries):
            raise _VerifyFailure("catalog_invalid")
        catalog_verified = True

    return {
        "schemaVersion": VERIFY_SCHEMA,
        "valid": True,
        "reason": "ok",
        "fileCount": len(entries),
        "databaseVerified": database_verified,
        "catalogVerified": catalog_verified,
    }


def verify_snapshot(snapshot_root: Any) -> Dict[str, Any]:
    """只读校验一份快照，返回有界结果 dict（``valid`` / ``reason`` 都是固定枚举）。

    完全按 ``manifest.json`` 的精确路径（限定在快照根之内）核对，拒绝缺失 / 被改 / 多余 /
    符号链接文件与不完整 / 畸形清单；核对 SQLite 完整 18 表契约与 ``integrity_check``，
    以及 catalog 引用材料拷贝的 sha256。**只读**：不创建、不修改、不删除任何文件。
    """
    try:
        return _verify_snapshot(snapshot_root)
    except _VerifyFailure as failure:
        return {
            "schemaVersion": VERIFY_SCHEMA,
            "valid": False,
            "reason": failure.reason,
            "fileCount": 0,
            "databaseVerified": False,
            "catalogVerified": False,
        }


# --------------------------------------------------------------------------- #
# Public: clone_snapshot
# --------------------------------------------------------------------------- #
def clone_snapshot(snapshot_root: Any, *, artifact_root: Any, output_name: Any) -> Dict[str, Any]:
    """从一份已校验快照在 ``artifact_root`` 下**新建**一份克隆（绝不覆盖源 / 活动库 / 服务）。

    顺序：先只读校验来源快照；再校验受信输出位置（必须不存在）并新建输出目录；逐文件流式
    拷贝并重新核对 sha256；写出克隆清单；再对本克隆做只读校验与恢复库 ``integrity_check`` +
    “含义等价”（schema / 行数 / 行内容摘要）。失败只删除**本次新建**的克隆目录。
    """
    verification = verify_snapshot(snapshot_root)
    if verification["valid"] is not True:
        raise RecoveryError("snapshot_invalid")
    root = _snapshot_root(snapshot_root)

    destination_root = _validate_artifact_root(artifact_root)
    name = _validate_output_name(output_name)
    output_dir = destination_root + "/" + name
    if os.path.lexists(output_dir):
        raise RecoveryError("output_exists")
    if output_dir == root or output_dir.startswith(root + "/") or root.startswith(output_dir + "/"):
        raise RecoveryError("output_invalid")

    try:
        manifest, _raw, manifest_digest = _read_manifest(root)
        entries = _validate_manifest(manifest)
    except _VerifyFailure:
        raise RecoveryError("snapshot_invalid") from None

    created = False
    try:
        os.mkdir(output_dir, MODE_DIR)
        created = True
        os.chmod(output_dir, MODE_DIR)

        for item in entries:
            relative = item["path"]
            target = output_dir + "/" + relative
            directory = os.path.dirname(target)
            if directory != output_dir:
                _make_directories(output_dir, directory)
            size, digest = _copy_new_file(
                root + "/" + relative, target, MODE_FILE, MAX_DESTINATION_BYTES
            )
            if digest != item["sha256"] or size != item["bytes"]:
                raise RecoveryError("clone_failed")

        clone_manifest = dict(manifest)
        clone_manifest["operation"] = "clone"
        clone_manifest["outputName"] = name
        clone_manifest["outputRoot"] = output_dir
        clone_manifest["sourceManifestDigest"] = manifest_digest
        _write_new_file(output_dir + "/" + MANIFEST_NAME, _manifest_bytes(clone_manifest), MODE_FILE)

        cloned = verify_snapshot(output_dir)
        if cloned["valid"] is not True:
            raise RecoveryError("clone_failed")

        database_relative = next(
            (item["path"] for item in entries if item["role"] == ROLE_DATABASE), None
        )
        if database_relative is not None:
            source_state = _database_state(root + "/" + database_relative)
            clone_state = _database_state(output_dir + "/" + database_relative)
            if source_state is None or clone_state is None or source_state != clone_state:
                raise RecoveryError("clone_failed")
            clone_connection = _open_readonly_db(output_dir + "/" + database_relative)
            try:
                if not _integrity_ok(clone_connection):
                    raise RecoveryError("clone_failed")
            finally:
                clone_connection.close()
    except RecoveryError:
        if created:
            shutil.rmtree(output_dir, ignore_errors=True)
        raise
    except BaseException:
        if created:
            shutil.rmtree(output_dir, ignore_errors=True)
        raise RecoveryError("clone_failed") from None

    return clone_manifest
