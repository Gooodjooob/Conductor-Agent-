"""DSH 隔离启动规格封装（D4；Python 3.9 标准库）。

本模块只**准备启动规格**：不启动任何进程、不联网、不读凭据、不改任何既有模块。

- 固定命令 ``[node, sdk_entry, "--profile", "sdk-minimal"]``。调用者只能注入
  ``node``（Node 可执行文件）与 ``sdk_entry``（DSH SDK 入口）；没有任何 argv /
  profile / 环境覆盖入口，带 ``command=`` / ``profile=`` 或位置参数调用会直接
  ``TypeError``。
- 目录校验、环境白名单与 Seatbelt 规则全部复用
  :func:`orchestrator.isolation.prepare_launch`（同一函数对象），本模块不复制
  策略生成代码。
- 本模块新增的校验（路径形状、符号链接段、可信 runtime_roots 归属与重叠、env 键）
  全部在调用 ``prepare_launch`` **之前**完成：非法输入不会新建私有 HOME、不会
  产生子进程。
- ``node`` / ``sdk_entry`` 以及三个目录参数 ``workspace`` / ``state_dir`` /
  ``runtime_roots`` 都在**任何 ``resolve`` 之前**在字面路径上逐段拒绝用户软链接；
  唯一例外是 macOS 固定系统别名：段名精确等于 ``/tmp`` / ``/var``，且该段
  ``realpath`` 精确等于 ``/private/tmp`` / ``/private/var``，漂移即拒绝。
- 失败时原样向上抛 ``IsolationError`` / ``IsolationUnavailable``，**不退回非沙箱
  命令**；``prepare_launch`` 的返回值还会被复核一次，非沙箱命令不会被返回。

边界声明：返回的 ``LaunchSpec`` 只证明"规格按既有隔离器生成"，**不证明**运行隔离
或真实 DSH 模型已通过；真实运行要等后续明确授权的 DSH 探针。
"""
import os
from pathlib import Path
import stat
from typing import Dict, Mapping, Optional, Sequence, Tuple

from orchestrator.isolation import (IsolationError, IsolationUnavailable,
                                   LaunchSpec, prepare_launch)

__all__ = ["DSH_SDK_PROFILE", "IsolationError", "IsolationUnavailable", "LaunchSpec",
           "prepare_dsh_launch"]

#: 唯一允许的 DSH profile。与 ``adapters.dsh_sdk.DSH_SDK_PROFILE`` 一致；本模块刻意
#: 不导入 adapters 包，保持零重量依赖、离线可测（drift 由测试内冻结输入 SHA 约束）。
DSH_SDK_PROFILE = "sdk-minimal"

#: 与 ``isolation.prepare_launch`` 实际生成的沙箱命令前缀一致；不一致即 fail closed，
#: 用来保证返回规格永远不会是非沙箱命令。
_SANDBOX_PREFIX = ("/usr/bin/sandbox-exec", "-p")

#: 镜像 ``orchestrator.isolation._SAFE_ENV``：只用于在调用 ``prepare_launch`` 前提前
#: 拒绝；权威校验仍在 ``prepare_launch`` 内（本模块不复制其策略生成代码）。
_SAFE_ENV_MIRROR = frozenset(("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"))

#: macOS 固定系统别名：路径段字面量 -> 唯一允许的 ``realpath`` 目标。
#: 只有段名精确等于 ``/tmp`` / ``/var``，并且该段 ``realpath`` 精确等于
#: ``/private/tmp`` / ``/private/var`` 时，才作为系统自带软链接放行；realpath
#: 漂移（指向别处或指向不存在目标）一律按用户 symlink 拒绝。
_SYSTEM_ALIAS_TARGETS = {"/tmp": "/private/tmp", "/var": "/private/var"}


def _within(path: Path, root: Path) -> bool:
    """``path`` 是否位于 ``root`` 之内（含相等）。"""
    return path == root or root in path.parents


def _overlap(left: Path, right: Path) -> bool:
    return _within(left, right) or _within(right, left)


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise IsolationError(label + " must be a non-empty string")
    if "\x00" in value:
        raise IsolationError(label + " must not contain NUL")
    return value


def _absolute(value: object, label: str) -> str:
    """绝对路径且不含 ``..`` 段；NUL/非字符串/空串同样拒绝。"""
    text = _text(value, label)
    if not text.startswith("/"):
        raise IsolationError(label + " must be absolute")
    if ".." in Path(text).parts:
        raise IsolationError(label + " must not contain '..' segments")
    return text


def _is_allowed_system_alias(cursor: Path) -> bool:
    """段名与 ``realpath`` 双双精确命中固定别名表，才算 macOS 系统别名。

    段名不在表里 -> 不是系统别名；在表里但 ``realpath`` 与固定目标不完全相等
    （漂移、指向别处、断裂）-> 也不算，交给调用方按用户 symlink 拒绝。
    """
    expected = _SYSTEM_ALIAS_TARGETS.get(str(cursor))
    if expected is None:
        return False
    try:
        return os.path.realpath(str(cursor)) == expected
    except OSError:  # pragma: no cover - 平台/权限差异
        return False


def _no_symlink_segments(value: str, label: str) -> None:
    """在**任何 resolve 之前**逐段拒绝用户可控的符号链接段（父目录段与最终段都查）。

    只看字面路径每一段的 ``lstat`` 结果，因此"先解析再放行"不会发生：指向别处的
    软链接、断裂软链接、指到可信目录的软链接一律当场拒绝。

    唯一例外只针对 macOS 固定系统别名：段名精确等于 ``/tmp`` / ``/var``，且该段
    ``realpath`` 精确等于 ``/private/tmp`` / ``/private/var``。
    """
    cursor = Path(Path(value).anchor)
    for part in Path(value).parts[1:]:
        cursor = cursor / part
        if cursor.is_symlink() and not _is_allowed_system_alias(cursor):
            raise IsolationError(label + " must not contain a symlink segment: " + str(cursor))


def _regular_file(value: str, label: str) -> None:
    try:
        info = os.lstat(value)
    except OSError as exc:
        raise IsolationError(label + " does not exist") from exc
    if not stat.S_ISREG(info.st_mode):
        raise IsolationError(label + " must be a regular file")


def _executable(value: str, label: str) -> None:
    if not os.access(value, os.X_OK):
        raise IsolationError(label + " must be executable")


def _resolved(value: str, label: str) -> Path:
    try:
        return Path(value).resolve()
    except OSError as exc:  # pragma: no cover - 平台/权限差异
        raise IsolationError(label + " cannot be resolved") from exc


def _roots(value: object) -> Tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise IsolationError("runtime_roots must be a sequence of absolute directories")
    texts = tuple(_absolute(item, "runtime_roots[%d]" % index)
                  for index, item in enumerate(value))
    if not texts:
        raise IsolationError("runtime_roots must not be empty")
    return texts


def _env(env: object) -> Dict[str, str]:
    """只接受 locale/timezone/terminal 覆盖；token/PATH/HOME/DSH_HOME/TMPDIR 一律拒绝。"""
    if env is None:
        return {}
    if not isinstance(env, Mapping):
        raise IsolationError("env must be a mapping of safe overrides")
    unknown = set(env) - _SAFE_ENV_MIRROR
    if unknown:
        raise IsolationError("environment keys outside explicit safe whitelist: "
                             + ", ".join(sorted(str(key) for key in unknown)))
    clean = {}
    for key, value in env.items():
        if not isinstance(value, str) or "\x00" in value:
            raise IsolationError("invalid environment value for " + str(key))
        clean[key] = value
    return clean


def _verify_spec(spec: object, entry_text: str, workspace_path: Path,
                 state_path: Path, node_path: Path) -> None:
    """复核隔离器返回值：必须仍是沙箱命令 + 固定 argv + 固定私有 HOME 布局。

    只做校验，不生成任何策略；任何不符即 ``IsolationError``（不退回非沙箱命令）。
    """
    if not isinstance(spec, LaunchSpec):
        raise IsolationError("unexpected launch spec type: refusing to return")
    command = list(spec.command)
    if len(command) < 4 or tuple(command[:2]) != _SANDBOX_PREFIX or command[2] != spec.profile:
        raise IsolationError("launch spec is not a sandboxed command: refusing to return")
    if command[3:] != [str(node_path), entry_text, "--profile", DSH_SDK_PROFILE]:
        raise IsolationError("launch spec command is not the fixed DSH argv")
    if spec.cwd != str(workspace_path):
        raise IsolationError("launch spec cwd is not the requested workspace")
    if spec.env.get("HOME") != spec.home:
        raise IsolationError("launch spec HOME is not the private home")
    if spec.env.get("DSH_HOME") != os.path.join(spec.home, "dsh"):
        raise IsolationError("launch spec DSH_HOME is not forced below the private home")
    for name in ("TMPDIR", "TMP", "TEMP"):
        if spec.env.get(name) != os.path.join(spec.home, "tmp"):
            raise IsolationError("launch spec " + name + " is not forced below the private home")
    if _overlap(state_path, workspace_path) or _overlap(state_path, _resolved(spec.home, "home")):
        raise IsolationError("control state is not outside the allowed roots")


def prepare_dsh_launch(*, node: str, sdk_entry: str, workspace: str,
                       runtime_roots: Sequence[str], state_dir: str,
                       env: Optional[Mapping[str, str]] = None) -> LaunchSpec:
    """准备 DSH 隔离启动规格并返回既有隔离器的 ``LaunchSpec``。

    参数（全部由可信调用者注入，不接受任意 argv/profile）：
    - ``node``：Node 可执行文件绝对路径；必须是普通文件、可执行、无符号链接段。
    - ``sdk_entry``：DSH SDK 入口绝对路径；必须是普通文件，位于某个可信
      ``runtime_roots`` 内，且不得位于可写 ``workspace`` 内。
    - ``workspace``：worker 可写工作目录（交给 ``prepare_launch`` 做目录校验）；
      每个路径段在任何 ``resolve`` 之前逐段拒绝用户 symlink。
    - ``runtime_roots``：只读依赖根；不得为 ``/``、不得包含用户 HOME，也不能与
      控制目录/workspace 重叠；同样在任何 ``resolve`` 之前逐段拒绝用户 symlink。
    - ``state_dir``：控制账本目录，必须位于所有允许根之外；同样在任何
      ``resolve`` 之前逐段拒绝用户 symlink。
    - ``env``：仅允许 locale/timezone/terminal 覆盖（``LANG``/``LC_ALL``/
      ``LC_CTYPE``/``TZ``/``TERM``）；token、``NODE_OPTIONS``、``PATH``、``HOME``、
      ``DSH_HOME``、``TMPDIR`` 覆盖一律拒绝。

    命令固定为 ``[node, sdk_entry, "--profile", "sdk-minimal"]``，不加任何其它
    argv；不启动进程、不联网、不读凭据。非法输入在调用 ``prepare_launch`` 前就被
    拒绝，因此不会新建私有 HOME、不会产生子进程。失败时原样抛出
    ``IsolationError`` / ``IsolationUnavailable``，不退回非沙箱命令。
    """
    node_text = _absolute(node, "node")
    entry_text = _absolute(sdk_entry, "sdk_entry")
    workspace_text = _absolute(workspace, "workspace")
    state_text = _absolute(state_dir, "state_dir")
    roots_text = _roots(runtime_roots)
    overrides = _env(env)

    # 1) 路径形状（全部在任何 resolve 之前）：符号链接段 / 存在性 / 普通文件 / 可执行位。
    _no_symlink_segments(node_text, "node")
    _no_symlink_segments(entry_text, "sdk_entry")
    _regular_file(node_text, "node")
    _regular_file(entry_text, "sdk_entry")
    _executable(node_text, "node")

    # 2) 三个目录参数同样先按字面路径逐段拒绝用户 symlink：判定发生在 resolve 之前，
    #    软链接不会被解析成可信目录后放行（断裂软链接同样拒绝）。
    _no_symlink_segments(workspace_text, "workspace")
    _no_symlink_segments(state_text, "state_dir")
    for index, root_text in enumerate(roots_text):
        _no_symlink_segments(root_text, "runtime_roots[%d]" % index)

    # 3) 目录归属与重叠：控制账本必须与 workspace 分离。
    workspace_path = _resolved(workspace_text, "workspace")
    state_path = _resolved(state_text, "state_dir")
    if _overlap(workspace_path, state_path):
        raise IsolationError("control state must be disjoint from the workspace")

    home = Path.home()
    roots = []
    for text in roots_text:
        path = _resolved(text, "runtime root")
        if path == Path(path.anchor):
            raise IsolationError("runtime_roots must not contain the filesystem root")
        if _within(home, path):
            raise IsolationError("runtime_roots must not contain the user HOME")
        if _overlap(path, workspace_path):
            raise IsolationError("runtime_roots must not overlap the workspace")
        if _overlap(path, state_path):
            raise IsolationError("runtime_roots must not overlap the control state_dir")
        if path in roots:
            raise IsolationError("runtime_roots must not contain duplicates")
        roots.append(path)

    entry_path = _resolved(entry_text, "sdk_entry")
    if _overlap(entry_path, workspace_path):
        raise IsolationError("sdk_entry must not live inside the writable workspace")
    if not any(_within(entry_path, root) for root in roots):
        raise IsolationError("sdk_entry must live inside a trusted runtime root")
    if _overlap(entry_path, state_path):
        raise IsolationError("sdk_entry must not live inside the control state_dir")
    node_path = _resolved(node_text, "node")
    if _overlap(node_path, state_path):
        raise IsolationError("node must not live inside the control state_dir")

    # 4) 交给既有隔离器：目录、环境白名单、Seatbelt 规则都不在此复制。
    command = [node_text, entry_text, "--profile", DSH_SDK_PROFILE]
    spec = prepare_launch(command, workspace_text, list(roots_text), state_text, overrides)
    _verify_spec(spec, entry_text, workspace_path, state_path, node_path)
    return spec
