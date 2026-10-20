"""Offline, fail-closed backup and restore for one orchestrator state root.

The backup lock is deliberately shared by Gateway/Scheduler/Runner. Restore
never edits absolute paths in contracts and is held until an operator
explicitly activates the restored directory.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
from contextlib import contextmanager

_LEASE = threading.local()


class RecoveryError(RuntimeError):
    pass


def _private_dir(path, create=False):
    p = Path(path)
    if not p.is_absolute() or p.is_symlink():
        raise RecoveryError("INVALID_STATE_ROOT")
    if create:
        p.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not p.is_dir():
        raise RecoveryError("STATE_ROOT_REQUIRED")
    st = p.stat()
    if st.st_uid != os.getuid() or st.st_mode & 0o077:
        raise RecoveryError("INSECURE_STATE_ROOT")
    return p


def _safe_lock(root):
    path = root / ".recovery.lock"
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077:
        os.close(fd)
        raise RecoveryError("UNSAFE_RECOVERY_LOCK")
    return fd


@contextmanager
def state_lease(state_dir, exclusive=False):
    """Acquire a process/shared-state lease; backups use exclusive=True."""
    root = _private_dir(state_dir, create=True)
    active = getattr(_LEASE, "active", None)
    if active and active[0] == str(root):
        # Nested transactions in one trusted thread reuse the outer lease.
        yield root
        return
    fd = _safe_lock(root)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        _LEASE.active = (str(root), fd, exclusive)
        yield root
    finally:
        _LEASE.active = None
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _walk(root):
    entries = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel == ".recovery.lock" or rel == "RESTORE_PENDING.json":
            continue
        try:
            st = path.lstat()
        except OSError as exc:
            raise RecoveryError("STATE_READ_FAILED") from exc
        if stat.S_ISLNK(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise RecoveryError("UNSAFE_STATE_ENTRY")
        if stat.S_ISDIR(st.st_mode):
            entries.append({"path": rel, "kind": "dir", "mode": stat.S_IMODE(st.st_mode)})
        elif stat.S_ISREG(st.st_mode):
            if st.st_nlink != 1:
                raise RecoveryError("UNSAFE_STATE_ENTRY")
            data = path.read_bytes()
            entries.append({"path": rel, "kind": "file", "mode": stat.S_IMODE(st.st_mode),
                            "size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        else:
            raise RecoveryError("UNSUPPORTED_STATE_ENTRY")
    return entries


def _manifest(entries):
    raw = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def verify_backup(backup_dir):
    root = _private_dir(backup_dir)
    manifest_path = root / "BACKUP_MANIFEST.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RecoveryError("BACKUP_MANIFEST_REQUIRED")
    try:
        doc = json.loads(manifest_path.read_text())
        entries, expected = doc["entries"], doc["manifestSha256"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RecoveryError("BACKUP_MANIFEST_INVALID") from exc
    if _manifest(entries) != expected:
        raise RecoveryError("BACKUP_MANIFEST_TAMPERED")
    actual = []
    for item in entries:
        rel = item.get("path")
        if not isinstance(rel, str) or rel.startswith("/") or ".." in Path(rel).parts:
            raise RecoveryError("BACKUP_PATH_INVALID")
        path = root / rel
        if item.get("kind") == "dir":
            if not path.is_dir() or path.is_symlink(): raise RecoveryError("BACKUP_ENTRY_MISSING")
            actual.append(item)
        elif item.get("kind") == "file":
            if not path.is_file() or path.is_symlink(): raise RecoveryError("BACKUP_ENTRY_MISSING")
            data = path.read_bytes()
            if len(data) != item.get("size") or hashlib.sha256(data).hexdigest() != item.get("sha256"):
                raise RecoveryError("BACKUP_CONTENT_TAMPERED")
            actual.append(item)
        else:
            raise RecoveryError("BACKUP_ENTRY_INVALID")
    if actual != entries:
        raise RecoveryError("BACKUP_ENTRY_SET_CHANGED")
    return {"manifestSha256": expected, "entries": entries}


def backup_state(state_dir, destination):
    source = _private_dir(state_dir)
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise RecoveryError("BACKUP_DESTINATION_EXISTS")
    if target.is_absolute() is False:
        raise RecoveryError("BACKUP_DESTINATION_MUST_BE_ABSOLUTE")
    with state_lease(source, exclusive=True):
        entries = _walk(source)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = Path(tempfile.mkdtemp(prefix=".backup-", dir=str(target.parent)))
        try:
            for item in entries:
                src = source / item["path"]
                dst = tmp / item["path"]
                if item["kind"] == "dir":
                    dst.mkdir(mode=item["mode"], parents=True, exist_ok=True)
                else:
                    dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, item["mode"] & 0o777)
                    with os.fdopen(fd, "wb") as out: out.write(src.read_bytes()); out.flush(); os.fsync(out.fileno())
            doc = {"format": 1, "sourceRoot": str(source), "entries": entries, "manifestSha256": _manifest(entries)}
            (tmp / "BACKUP_MANIFEST.json").write_text(json.dumps(doc, sort_keys=True, indent=2))
            os.chmod(tmp / "BACKUP_MANIFEST.json", 0o600)
            os.replace(str(tmp), str(target))
            tmp = None
        finally:
            if tmp and tmp.exists(): shutil.rmtree(tmp)
    return verify_backup(target)


def restore_state(backup_dir, destination):
    source = Path(backup_dir)
    manifest = verify_backup(source)
    target = Path(destination)
    if target.exists() or target.is_symlink():
        raise RecoveryError("RESTORE_DESTINATION_EXISTS")
    if not target.is_absolute(): raise RecoveryError("RESTORE_DESTINATION_MUST_BE_ABSOLUTE")
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".restore-", dir=str(target.parent)))
    try:
        for item in manifest["entries"]:
            src, dst = source / item["path"], tmp / item["path"]
            if item["kind"] == "dir": dst.mkdir(mode=item["mode"], parents=True, exist_ok=True)
            else:
                dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                fd = os.open(str(dst), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, item["mode"] & 0o777)
                with os.fdopen(fd, "wb") as out: out.write(src.read_bytes()); out.flush(); os.fsync(out.fileno())
        hold = {"format": 1, "state": "restore_pending", "sourceRoot": manifest.get("sourceRoot"),
                "destinationRoot": str(target), "manifestSha256": manifest["manifestSha256"]}
        (tmp / "RESTORE_PENDING.json").write_text(json.dumps(hold, sort_keys=True, indent=2))
        os.chmod(tmp / "RESTORE_PENDING.json", 0o600)
        os.replace(str(tmp), str(target)); tmp = None
    finally:
        if tmp and tmp.exists(): shutil.rmtree(tmp)
    return hold


def activate_restored_state(state_dir, manifest_sha256):
    root = _private_dir(state_dir)
    marker = root / "RESTORE_PENDING.json"
    if marker.is_symlink() or not marker.is_file(): raise RecoveryError("RESTORE_HOLD_NOT_FOUND")
    try: doc = json.loads(marker.read_text())
    except (OSError, ValueError) as exc: raise RecoveryError("RESTORE_HOLD_INVALID") from exc
    if doc.get("manifestSha256") != manifest_sha256: raise RecoveryError("RESTORE_MANIFEST_MISMATCH")
    marker.unlink()
    return {"state": "active", "manifestSha256": manifest_sha256}
