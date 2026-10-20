"""Fail-closed, offline macOS worker isolation (Python 3.9 stdlib only).

This is a local P0 boundary, not a portable production container runtime.
The trusted launcher owns runtime_roots and state_dir; do not derive either
from a worker request. No network, credentials, controller descriptors or
controller files are made available to the worker. Descendants inherit Seatbelt.
"""
import dataclasses
import json
import os
from pathlib import Path
import platform
import stat
import subprocess
import tempfile
from typing import Dict, List, Mapping, Optional, Sequence


class IsolationError(RuntimeError):
    pass


class IsolationUnavailable(IsolationError):
    pass


@dataclasses.dataclass(frozen=True)
class LaunchSpec:
    command: List[str]
    cwd: str
    env: Dict[str, str]
    home: str
    profile: str


_SYSTEM_ROOTS = ("/usr/lib", "/System/Library", "/usr/bin", "/bin")
_SAFE_ENV = frozenset(("LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM"))


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _overlap(left: Path, right: Path) -> bool:
    return _within(left, right) or _within(right, left)


def _directory(value: str, label: str) -> Path:
    original = Path(value)
    if not original.is_absolute():
        raise IsolationError(label + " must be absolute")
    try:
        result = original.resolve(strict=True)
    except OSError as exc:
        raise IsolationError(label + " does not exist") from exc
    if not result.is_dir():
        raise IsolationError(label + " must be a directory")
    return result


def _literal(path: Path) -> str:
    # JSON string quoting also safely quotes SBPL path strings.
    return json.dumps(str(path), ensure_ascii=True)


def prepare_launch(command: Sequence[str], workspace: str,
                   runtime_roots: Sequence[str], state_dir: str,
                   env: Optional[Mapping[str, str]] = None) -> LaunchSpec:
    """Create a Seatbelt command; never falls back to an unsandboxed command.

    Private HOME is a retained temporary directory, intentionally outside the
    controller state. The supervisor may remove it after collecting results.
    Only locale/timezone/terminal env overrides are accepted; DSH_HOME is forced
    below private HOME. Runtime roots are read-only, workspace/HOME are writable.
    Keep control files and other sensitive files outside all permitted roots.
    """
    if platform.system() != "Darwin" or not os.access("/usr/bin/sandbox-exec", os.X_OK):
        raise IsolationUnavailable("verified macOS sandbox-exec is required; unsupported host")
    if isinstance(command, (str, bytes)) or not command or any(not isinstance(x, str) for x in command):
        raise IsolationError("command must be a non-empty argv list")
    if not Path(command[0]).is_absolute():
        raise IsolationError("command executable must be absolute")
    executable = Path(command[0]).resolve(strict=True)
    if not executable.is_file() or not os.access(str(executable), os.X_OK):
        raise IsolationError("command executable must be an executable file")
    workspace_path = _directory(workspace, "workspace")
    state_path = _directory(state_dir, "state_dir")
    roots = [_directory(str(p), "runtime root") for p in runtime_roots]
    roots += [Path(p).resolve() for p in _SYSTEM_ROOTS]
    if any(_overlap(p, state_path) for p in [workspace_path] + roots):
        raise IsolationError("control state must be disjoint from all allowed roots")
    if any(_overlap(workspace_path, p) for p in roots):
        raise IsolationError("workspace must not overlap read-only runtime roots")
    if not any(_within(executable, p) for p in [workspace_path] + roots):
        raise IsolationError("executable must be inside an explicit runtime root or workspace")
    for directory, _, files in os.walk(str(workspace_path), followlinks=False):
        for name in files:
            metadata = os.lstat(os.path.join(directory, name))
            if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
                raise IsolationError("workspace contains pre-existing hardlinked file")
    overrides = dict(env or {})
    if set(overrides) - _SAFE_ENV:
        raise IsolationError("environment keys outside explicit safe whitelist")
    if any(not isinstance(v, str) or "\x00" in v for v in overrides.values()):
        raise IsolationError("invalid environment value")

    worker_home = Path(tempfile.mkdtemp(prefix="orchestrator-worker-", dir="/private/tmp")).resolve()
    os.chmod(str(worker_home), 0o700)
    worker_tmp = worker_home / "tmp"
    worker_tmp.mkdir(mode=0o700)
    dsh_home = worker_home / "dsh"
    dsh_home.mkdir(mode=0o700)
    read_roots = list(dict.fromkeys(roots + [workspace_path, worker_home]))
    ancestors = set()
    for root in read_roots:
        ancestors.update(root.parents)
    # dyld needs literal directory reads (including /); this does not grant
    # recursive content reads outside the explicitly allowed subpaths.
    profile = ["(version 1)", "(deny default)", "(allow process-exec)",
               "(allow process-fork)",
               # Default deny and sysctl-read denial alone did not block
               # KERN_PROCARGS2. Explicit process-info denial is essential.
               '(allow sysctl-read (sysctl-name "kern.argmax") '
               '(sysctl-name "hw.ncpu") (sysctl-name "hw.activecpu") '
               '(sysctl-name "hw.memsize") (sysctl-name "hw.pagesize") '
               '(sysctl-name "hw.pagesize_compat") (sysctl-name "kern.osrelease") '
               '(sysctl-name "kern.ostype") (sysctl-name "kern.hostname") '
               '(sysctl-name "kern.version") (sysctl-name "hw.machine"))',
               "(deny process-info* (require-not (target self)))",
               "(allow process-info* (target self))"]
    profile.append("(allow file-read* " + " ".join("(literal %s)" % _literal(p) for p in sorted(ancestors)) + ")")
    profile.append("(allow file-read* file-map-executable " + " ".join("(subpath %s)" % _literal(p) for p in read_roots) + ")")
    profile.append("(allow file-write* (subpath %s) (subpath %s))" % (_literal(workspace_path), _literal(worker_home)))
    profile.append('(allow file-read* (literal "/dev/null") (literal "/dev/urandom") (literal "/dev/random"))')
    profile.append('(allow file-write-data (literal "/dev/null"))')
    profile.append("(deny file-read* file-write* (subpath %s))" % _literal(state_path))
    profile += ["(deny network*)", "(deny signal)"]
    profile_text = "\n".join(profile)
    clean_env = {"HOME": str(worker_home), "TMPDIR": str(worker_tmp),
                 "TMP": str(worker_tmp), "TEMP": str(worker_tmp),
                 "DSH_HOME": str(dsh_home), "PATH": "/usr/bin:/bin",
                 "LANG": "en_US.UTF-8", "PYTHONNOUSERSITE": "1",
                 "PYTHONDONTWRITEBYTECODE": "1"}
    clean_env.update(overrides)
    return LaunchSpec(["/usr/bin/sandbox-exec", "-p", profile_text,
                       str(executable)] + list(command[1:]),
                      str(workspace_path), clean_env, str(worker_home), profile_text)


def launch(command: Sequence[str], workspace: str, runtime_roots: Sequence[str],
           state_dir: str, env: Optional[Mapping[str, str]] = None, **kwargs):
    """Start a worker with closed descriptors and a new process session.

    The caller owns the child and its private HOME (process.isolation_home).
    stdin/stdout/stderr may be pipes or supervisor-owned log descriptors only.
    """
    forbidden = {"shell", "env", "cwd", "pass_fds", "close_fds", "preexec_fn",
                 "executable", "start_new_session"}
    if forbidden.intersection(kwargs):
        raise IsolationError("unsafe subprocess override")
    spec = prepare_launch(command, workspace, runtime_roots, state_dir, env)
    process = subprocess.Popen(spec.command, cwd=spec.cwd, env=spec.env,
                               close_fds=True, start_new_session=True, **kwargs)
    process.isolation_home = spec.home
    return process
