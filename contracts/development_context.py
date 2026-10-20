"""D4 §14.1：DSH 开发上下文离线装配（任务卡 D4-DSH-DEV-CONTEXT-001-R1）。

纯只读、纯离线模块：把「中央开发角色 + 项目规则 + 任务卡」三份原文安全装配成
可检查的上下文对象，供后续 DSH 启动接线（另卡，见接口合同 §14.2）使用。

安全边界（冻结，R1 修订）：
- 固定角色 skill-worker；角色唯一中央路径按本模块所在 conductor 根推导为固定绝对
  路径 `<conductor>/sales-dev/references/dsh-development-role.md`，入参 role_path
  必须与该路径全等；不做尾段/后缀匹配，任何别处同尾段文件一律拒绝。
- 项目输入固定 `<workspace>/AGENTS.md`；不加载父目录、嵌套规则、SOUL 或递归展开 Skill。
- 任务卡必须落在调用方（受控入口）注入的 allowed_task_roots 内；调用方不可从任务
  正文选择角色路径或 expected digests。
- 路径规则：绝对路径、无 `..` 段；workspace/task/role/allowed roots 在任何 OS 调用
  之前统一拒绝内嵌 NUL 与无法被文件系统编码的字符串（R2 修订：仅捕获明确的
  UnicodeEncodeError，不吞内部错误）；逐段拒绝用户符号链接（含父目录）；系统目录
  别名仅保留固定明示例外（macOS `/tmp`、`/var`），
  其余 realpath 变化不放行；项目路径不做 realpath，保留符号链接信息。
- 读取无先检查后打开的竞态模式：以目录 fd 逐段打开（`O_DIRECTORY|O_NOFOLLOW`；
  macOS 对固定系统别名（/tmp、/var）的目录段返回 ENOTDIR 而非 ELOOP，R1 起两者
  都只经「字面前缀 + realpath 全等固定别名对 + dev/ino 复核」放行），
  末段 `O_NOFOLLOW|O_NONBLOCK` 打开后用 `fstat` 锁定 regular file，限制读取
  MAX_FILE_BYTES+1 字节，读入后复核实际字节大小与锁定 inode 元数据一致；
  不可能读到 FIFO/设备或发生不受限 read。
- 三份输入都要求：普通文件、非空、严格 UTF-8；逐字节 SHA-256 与 expected_digests
  精确比对，不规范化原文；digest 形态用 fullmatch 校验，拒绝尾换行等变体。
- 大小上限：单文件 64 KiB，三者合计 192 KiB；超限拒绝，不静默截断。
- 不建库、不写 attempt/snapshot、不创建临时文件、不调宿主/网络/子进程/模型。
- 错误统一为 ContractError('invalid_task')，details 只含受控 reason/field，
  不回显路径解析细节或文件正文；reason 固定为本文末尾列出的六个稳定值。
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import stat

from contracts.schemas import ContractError

# ---------------------------------------------------------------------------
# 冻结常量（接口合同 §14.1）
# ---------------------------------------------------------------------------

SCHEMA_VERSION = "dsh-development-context.v1"
ROLE_ID = "skill-worker"

# 中央角色来源相对段；完整路径由本模块所在 conductor 根推导，不依赖 workspace/入参
ROLE_SOURCE_RELATIVE_PARTS = ("sales-dev", "references", "dsh-development-role.md")

# 本文件位于 <conductor>/shared/agent-orchestrator/contracts/ 下：
# 本文件目录 -> agent-orchestrator -> shared -> conductor 根
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_MODULE_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_THIS_DIR)))
ROLE_SOURCE_ABSOLUTE = os.path.join(_MODULE_ROOT, *ROLE_SOURCE_RELATIVE_PARTS)

# 项目规则固定为 workspace 根目录的 AGENTS.md（首版不递归、不加载父目录）
PROJECT_RULES_BASENAME = "AGENTS.md"

MAX_FILE_BYTES = 64 * 1024    # 单文件 64 KiB
MAX_TOTAL_BYTES = 192 * 1024  # 三文件合计 192 KiB

# sources 固定次序（role -> project -> taskCard）
SOURCE_KINDS = ("role", "project", "taskCard")

# 稳定 reason（合同 §14.1 固定六个，不新增）
REASON_INPUT_INVALID = "context_input_invalid"
REASON_PATH_REJECTED = "context_path_rejected"
REASON_FILE_MISSING = "context_file_missing"
REASON_FILE_INVALID = "context_file_invalid"
REASON_DIGEST_MISMATCH = "context_digest_mismatch"
REASON_TOO_LARGE = "context_too_large"

# 系统目录别名固定例外：仅这两对，按（字面路径, realpath）全等匹配；
# 其余任何 realpath 变化都按用户符号链接拒绝，不做通用"系统别名"识别。
_SYSTEM_ALIAS_EXCEPTIONS = frozenset({
    ("/tmp", "/private/tmp"),
    ("/var", "/private/var"),
})

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")

# systemPrompt 装配用的固定权限边界（不受任何输入内容影响）
PERMISSION_BOUNDARY_SECTION = (
    "## 固定权限边界（装配层追加，任务内容不能修改）\n\n"
    "- 本轮权限以受控配置为准：只写授权路径，不联网、不做业务写入、不发布、不同步。\n"
    "- 工具被拒绝时如实记录 NOT_RUN，不绕过、不申请扩大权限。\n"
    "- 不自任 CoX/主控、QA、发布者；未知或冲突时停止并上报。\n"
    "- 回执与报告如实记录，不编造结果、数据或精确执行时间。"
)


def _reject(reason, field):
    """统一失败出口：只暴露受控 reason/field，不回显路径内容或文件正文。"""
    raise ContractError("invalid_task", details={"reason": reason, "field": field})


# ---------------------------------------------------------------------------
# 路径基础校验（component-aware；不用字符串前缀冒充目录边界）
# ---------------------------------------------------------------------------

def _validate_absolute_no_dotdot(path, field):
    """路径基础校验统一入口（R2 修订）。

    四种路径输入（workspace/task_card_path/role_path/allowed_task_roots）都经过
    本函数，因此在任何 OS 调用之前先拒绝内嵌 NUL 与无法被 os.fsencode 编码的
    字符串，统一映射 ContractError('invalid_task', reason=context_path_rejected)，
    避免裸 ValueError 泄漏、违反统一错误合同。编码错误只捕获明确的
    UnicodeEncodeError，不用 catch-all 吞掉内部错误。
    """
    if not isinstance(path, str) or not path or not path.startswith("/"):
        _reject(REASON_PATH_REJECTED, field)
    if "\x00" in path:
        _reject(REASON_PATH_REJECTED, field)
    try:
        os.fsencode(path)
    except UnicodeEncodeError:
        _reject(REASON_PATH_REJECTED, field)
    if ".." in path.split("/"):
        _reject(REASON_PATH_REJECTED, field)


def _split_components(path):
    """绝对路径 -> 非空段列表（保留空段剔除，"." 由词法包含比对再剔除）。"""
    return [part for part in path.split("/") if part]


def _policy_check_no_user_symlink(path, field):
    """逐段检查用户符号链接；仅固定系统别名例外放行。

    这是策略层检查；实际打开时的 O_NOFOLLOW 是机制层兜底，两层独立生效。
    """
    parts = _split_components(path)
    for index, _part in enumerate(parts):
        prefix = "/" + "/".join(parts[: index + 1])
        if os.path.islink(prefix):
            if (prefix, os.path.realpath(prefix)) not in _SYSTEM_ALIAS_EXCEPTIONS:
                _reject(REASON_PATH_REJECTED, field)


def _within_roots_lexical(path, roots):
    """词法 component 包含判断：path 的段序列以某 root 的段序列开头（或相等）。

    入参路径与 roots 均已通过绝对路径/`..`/用户符号链接校验，词法比对即安全；
    不做 realpath，避免解析掉符号链接信息后放宽边界。
    """
    parts = [part for part in _split_components(path) if part != "."]
    for root in roots:
        root_parts = [part for part in _split_components(root) if part != "."]
        if parts[: len(root_parts)] == root_parts:
            return True
    return False


def _resolve_task_roots(allowed_task_roots):
    """校验调用方注入的任务根：绝对、无 `..`、无用户符号链接；不做 realpath。"""
    if not isinstance(allowed_task_roots, (list, tuple)) or not allowed_task_roots:
        _reject(REASON_INPUT_INVALID, "allowed_task_roots")
    roots = []
    for entry in allowed_task_roots:
        _validate_absolute_no_dotdot(entry, "allowed_task_roots")
        _policy_check_no_user_symlink(entry, "allowed_task_roots")
        roots.append(entry)
    return roots


# ---------------------------------------------------------------------------
# expected_digests：恰有 role/project/taskCard 三项，格式 sha256:<64 位小写十六进制>
# 形态用 fullmatch 校验，拒绝尾换行等 `$` 锚点会放过的变体
# ---------------------------------------------------------------------------

def _validate_expected_digests(expected_digests):
    if not isinstance(expected_digests, dict):
        _reject(REASON_INPUT_INVALID, "expected_digests")
    if set(expected_digests.keys()) != set(SOURCE_KINDS):
        _reject(REASON_INPUT_INVALID, "expected_digests")
    for kind in SOURCE_KINDS:
        value = expected_digests[kind]
        if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
            _reject(REASON_INPUT_INVALID, "expected_digests." + kind)


# ---------------------------------------------------------------------------
# 受控读取单份输入（无竞态、无不受限 read）
#
# 步骤：绝对/`..` 校验 -> 词法受控根包含 -> 逐段用户符号链接策略检查 ->
# 以目录 fd 逐段打开（目录段 O_DIRECTORY|O_NOFOLLOW，末段 O_NOFOLLOW|O_NONBLOCK；
# 命中符号链接时仅固定系统别名例外按 dev/ino 复核放行）-> fstat 锁定 regular file
# -> 限读 MAX_FILE_BYTES+1 -> 复核读入长度与锁定 inode 当前大小一致 ->
# 非空、严格 UTF-8。任何 OSError 统一映射到六个冻结 reason，不泄漏异常原文。
# ---------------------------------------------------------------------------

def _open_component(component, dir_fd, is_last, field, parts, index):
    """打开单一路径段并返回新 fd；命中符号链接时仅固定系统别名例外放行。"""
    flags = os.O_RDONLY
    if is_last:
        # O_NONBLOCK：FIFO 立即返回而不是阻塞等写端；对 regular file 无副作用
        flags |= os.O_NOFOLLOW | os.O_NONBLOCK
    else:
        flags |= os.O_DIRECTORY | os.O_NOFOLLOW
    try:
        return os.open(component, flags, dir_fd=dir_fd)
    except OSError as exc:
        # R1 修复（D4-DSH-CONTEXT-SYSTEM-ALIAS-R1）：macOS 上目录段以
        # O_DIRECTORY|O_NOFOLLOW 打开固定系统别名（/tmp、/var 均为符号链接）
        # 返回 ENOTDIR，而不是 ELOOP；只处理 ELOOP 会把合法系统临时目录误拒绝。
        # ELOOP/ENOTDIR 都只在「字面前缀 + realpath 全等匹配固定别名对」时进入
        # 例外分支，并继续以 dev/ino 复核；非固定别名的 ENOTDIR（普通非目录段）
        # 不当作别名处理，保持原有 OSError 语义交由上层统一映射为受控 reason。
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            prefix = "/" + "/".join(parts[: index + 1])
            if (prefix, os.path.realpath(prefix)) in _SYSTEM_ALIAS_EXCEPTIONS:
                fd = os.open(component, flags & ~os.O_NOFOLLOW, dir_fd=dir_fd)
                try:
                    opened = os.fstat(fd)
                    expected = os.stat(prefix)
                except OSError:
                    os.close(fd)
                    _reject(REASON_PATH_REJECTED, field)
                if (opened.st_dev, opened.st_ino) != (expected.st_dev, expected.st_ino):
                    os.close(fd)
                    _reject(REASON_PATH_REJECTED, field)
                return fd
            if exc.errno == errno.ELOOP:
                _reject(REASON_PATH_REJECTED, field)
            raise
        if exc.errno == errno.ENOENT:
            _reject(REASON_FILE_MISSING, field)
        raise


def _safe_read_file(path, field, within_roots=None):
    """安全读取；返回 (字面路径, 原始字节, 文本)。所有 fd 保证关闭。"""
    _validate_absolute_no_dotdot(path, field)
    if within_roots is not None and not _within_roots_lexical(path, within_roots):
        _reject(REASON_PATH_REJECTED, field)
    _policy_check_no_user_symlink(path, field)

    parts = _split_components(path)
    fds_to_close = []
    raw = b""
    st_after = None
    try:
        dir_fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
        fds_to_close.append(dir_fd)
        for index, part in enumerate(parts):
            next_fd = _open_component(
                part, dir_fd, index == len(parts) - 1, field, parts, index
            )
            # 旧目录 fd 不在此手动关闭；统一由 finally 关闭，避免重复 close
            fds_to_close.append(next_fd)
            dir_fd = next_fd
        # dir_fd 现在锁定最终目标；先判形态再读取
        st_before = os.fstat(dir_fd)
        if not stat.S_ISREG(st_before.st_mode):
            _reject(REASON_FILE_INVALID, field)
        if st_before.st_size == 0:
            _reject(REASON_FILE_INVALID, field)
        if st_before.st_size > MAX_FILE_BYTES:
            _reject(REASON_TOO_LARGE, field)
        # 限读 MAX+1：多读 1 字节用于发现超限，绝不不受限 read
        chunks = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(dir_fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        st_after = os.fstat(dir_fd)
    except ContractError:
        raise
    except OSError:
        _reject(REASON_FILE_INVALID, field)
    finally:
        for fd in fds_to_close:
            try:
                os.close(fd)
            except OSError:
                pass

    # 受控读入后复核：实际读到的字节数必须与锁定 inode 当前大小一致；
    # 若 stat 与 read 之间文件被替换/截断/追加，这里拒绝而不是使用不一致内容
    if len(raw) != st_after.st_size:
        _reject(REASON_FILE_INVALID, field)
    if len(raw) == 0:
        _reject(REASON_FILE_INVALID, field)
    if len(raw) > MAX_FILE_BYTES:
        _reject(REASON_TOO_LARGE, field)
    try:
        text = raw.decode("utf-8")  # 严格 UTF-8；BOM 不剥、不做任何规范化
    except UnicodeDecodeError:
        _reject(REASON_FILE_INVALID, field)
    return path, raw, text


# ---------------------------------------------------------------------------
# 主接口（合同 §14.1 冻结签名；keyword-only，全部由受控入口提供）
# ---------------------------------------------------------------------------

def build_development_context(*, workspace, task_card_path, role_path,
                              allowed_task_roots, expected_digests):
    """安全装配 DSH 开发上下文；任何失败 fail closed，不产生部分结果。"""
    _validate_expected_digests(expected_digests)

    # 1) workspace：绝对、无 `..`、逐段无用户符号链接；项目规则固定 <workspace>/AGENTS.md
    _validate_absolute_no_dotdot(workspace, "workspace")
    _policy_check_no_user_symlink(workspace, "workspace")
    workspace_base = workspace.rstrip("/")
    project_path = (workspace_base or "") + "/" + PROJECT_RULES_BASENAME

    # 2) 角色固定中央路径：与本模块位置推导出的固定绝对路径全等；不做尾段匹配
    _validate_absolute_no_dotdot(role_path, "role_path")
    if role_path != ROLE_SOURCE_ABSOLUTE:
        _reject(REASON_PATH_REJECTED, "role_path")

    # 3) 任务卡受控根由调用方注入（不 realpath，保留符号链接信息）
    task_roots = _resolve_task_roots(allowed_task_roots)

    # 4) 受控读取三份输入（每份先过单文件上限，再统一核对合计上限）
    role_used, role_raw, role_text = _safe_read_file(role_path, "role_path")
    project_used, project_raw, project_text = _safe_read_file(
        project_path, "workspace", within_roots=[workspace],
    )
    task_used, task_raw, task_text = _safe_read_file(
        task_card_path, "task_card_path", within_roots=task_roots,
    )
    total_bytes = len(role_raw) + len(project_raw) + len(task_raw)
    if total_bytes > MAX_TOTAL_BYTES:
        _reject(REASON_TOO_LARGE, "total_bytes")

    # 5) 逐字节 SHA 比对；不匹配拒绝（内容漂移 / expected digest 错误统一为一类 reason）
    actual = {
        "role": "sha256:" + hashlib.sha256(role_raw).hexdigest(),
        "project": "sha256:" + hashlib.sha256(project_raw).hexdigest(),
        "taskCard": "sha256:" + hashlib.sha256(task_raw).hexdigest(),
    }
    for kind in SOURCE_KINDS:
        if actual[kind] != expected_digests[kind]:
            _reject(REASON_DIGEST_MISMATCH, "expected_digests." + kind)

    # 6) 装配：systemPrompt = 角色原文 + 固定权限边界；taskContext 明确标注项目规则与任务卡
    sources = [
        {"kind": "role", "path": role_used, "sha256": actual["role"], "bytes": len(role_raw)},
        {"kind": "project", "path": project_used, "sha256": actual["project"], "bytes": len(project_raw)},
        {"kind": "taskCard", "path": task_used, "sha256": actual["taskCard"], "bytes": len(task_raw)},
    ]
    result = {
        "schemaVersion": SCHEMA_VERSION,
        "role": ROLE_ID,
        "systemPrompt": role_text.rstrip("\n") + "\n\n" + PERMISSION_BOUNDARY_SECTION,
        "taskContext": {
            "projectRules": {
                "label": "项目规则（workspace/AGENTS.md 原文，未经解析）",
                "path": project_used,
                "text": project_text,
            },
            "taskCard": {
                "label": "本轮任务卡（受控任务根内原文，未经解析）",
                "path": task_used,
                "text": task_text,
            },
        },
        "sources": sources,
    }

    # 7) contextDigest：除自身外的返回对象，规范 JSON UTF-8 SHA-256，可重复复算
    canonical = json.dumps(
        result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    result["contextDigest"] = (
        "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    )
    return result
