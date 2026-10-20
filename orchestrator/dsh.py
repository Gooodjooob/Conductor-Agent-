"""Durable one-attempt DSH stdio adapter (preview protocol, Python 3.9).

This module is a trusted transport, not a sandbox. Supply the restricted command
and explicit environment from the isolation launcher. Never give workers access
to store_dir. An uncertain send is never retried, including after owner death.
"""
import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import queue
import signal
import sqlite3
import stat
import subprocess
import threading
import time
import uuid


class AdapterError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class DSHAdapter:
    TERMINAL = {"failed", "unknown", "awaiting_validation"}
    MAX_FRAME = 2 * 1024 * 1024

    def __init__(self, store_dir, command, workspace, env, timeout=10,
                 provider="deepseek-official", model="deepseek-chat", execution_timeout=300):
        if not command or not isinstance(command, (list, tuple)):
            raise ValueError("COMMAND_REQUIRED")
        if not isinstance(env, dict) or "HOME" not in env or timeout <= 0 or execution_timeout <= 0:
            raise ValueError("EXPLICIT_ENV_HOME_AND_TIMEOUT_REQUIRED")
        raw_store = Path(store_dir).absolute()
        # macOS's system aliases are expected; user-controlled symlink segments
        # are not. Do not silently resolve a store into an unrelated location.
        cursor = Path(raw_store.anchor)
        for part in raw_store.parts[1:]:
            cursor = cursor / part
            if cursor.is_symlink() and cursor not in {Path('/tmp'), Path('/var'), Path('/etc')}:
                raise AdapterError("STORE_SYMLINK_FORBIDDEN")
        self.store = raw_store.resolve()
        self.workspace = Path(workspace).resolve(strict=True)
        if self.store == self.workspace or self.workspace in self.store.parents:
            raise ValueError("STORE_MUST_BE_OUTSIDE_WORKSPACE")
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        info = self.store.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise AdapterError("STORE_MUST_BE_PRIVATE_OWNED_DIRECTORY")
        self._store_identity = (info.st_dev, info.st_ino)
        self.command, self.env = list(command), dict(env)
        self.timeout, self.provider, self.model = timeout, provider, model
        self.execution_timeout = execution_timeout
        self._launch_digest = self._config_digest()
        self.db_path = self.store / "receipt.sqlite"
        self._mutex = threading.RLock()
        self._close_mutex = threading.Lock()
        self._stopped = threading.Event()
        self._waiters, self._counter = {}, 0
        self._responded = set()
        self.child = self.reader = self.stderr = None
        self.ready = self._closing = self._closed = False
        self._protocol_fault = None
        self._lock = os.fdopen(self._private_file("owner.lock"), "a+b")
        try:
            fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.owner = True
        except BlockingIOError:
            self.owner = False
        try:
            self._initialize_store()
        except Exception:
            self._lock.close()
            raise

    def _initialize_store(self):
        with self._connect() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS config (
                slot INTEGER PRIMARY KEY CHECK(slot=1), digest TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS attempt (
                slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT UNIQUE,
                contract TEXT NOT NULL, receipt TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS frames (
                id INTEGER PRIMARY KEY AUTOINCREMENT, observed REAL NOT NULL,
                raw TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY, body TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT digest FROM config WHERE slot=1").fetchone()
            if row and row[0] != self._launch_digest:
                raise AdapterError("LAUNCH_CONFIG_CONFLICT")
            if not row:
                db.execute("INSERT INTO config VALUES (1,?)", (self._launch_digest,))
            # Owning this lock proves the old controller no longer owns it. It
            # does not prove whether its SDK completed: preserve uncertainty.
            if self.owner:
                row = db.execute("SELECT receipt FROM attempt").fetchone()
                if row:
                    receipt = json.loads(row[0])
                    if receipt["state"] not in self.TERMINAL or (receipt["state"] == "awaiting_validation" and not receipt.get("runtimeClosed")):
                        receipt.update(state="unknown", reason="OWNER_LOST")
                        self._save(db, receipt)

    def _config_digest(self):
        # Environment may contain credentials; only its digest is persisted.
        config = {"command": self.command, "env": self.env,
                  "workspace": str(self.workspace), "provider": self.provider,
                  "model": self.model, "timeout": self.timeout,
                  "executionTimeout": self.execution_timeout}
        return hashlib.sha256(_json(config).encode()).hexdigest()

    def _check_store(self):
        cursor = Path(self.store.anchor)
        for part in self.store.parts[1:]:
            cursor = cursor / part
            if cursor.is_symlink():
                raise AdapterError("STORE_SYMLINK_FORBIDDEN")
        info = self.store.lstat()
        if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self._store_identity or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            raise AdapterError("STORE_IDENTITY_OR_PERMISSIONS_CHANGED")

    def _private_file(self, name, create=True, append=False):
        self._check_store()
        try:
            fd = os.open(str(self.store / name), os.O_RDWR | (os.O_CREAT if create else 0) | (os.O_APPEND if append else 0) | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        except FileNotFoundError:
            if not create:
                return None
            raise
        except OSError as error:
            raise AdapterError("UNSAFE_STORE_FILE:" + name) from error
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
            os.close(fd)
            raise AdapterError("STORE_FILE_MUST_BE_PRIVATE_REGULAR:" + name)
        return fd

    @contextmanager
    def _connect(self):
        self._check_store()
        # sqlite3 lacks a Python openat API; pin/check the private parent and
        # pre-create its files without following links before SQLite opens them.
        os.close(self._private_file("receipt.sqlite"))
        for name in ("receipt.sqlite-journal", "receipt.sqlite-wal", "receipt.sqlite-shm"):
            path = self.store / name
            if path.exists() or path.is_symlink():
                fd = self._private_file(name, create=False)
                if fd is not None:
                    os.close(fd)
        db = sqlite3.connect(self.db_path.as_uri() + "?mode=rw", timeout=10, uri=True)
        db.execute("PRAGMA synchronous=FULL")
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _save(db, receipt):
        db.execute("UPDATE attempt SET receipt=? WHERE slot=1", (_json(receipt),))

    def _receipt(self, db):
        row = db.execute("SELECT receipt FROM attempt WHERE slot=1").fetchone()
        return json.loads(row[0]) if row else None

    def probe(self):
        return {"transport": "stdio-jsonrpc", "protocolStability": "preview",
                "executableExists": Path(self.command[0]).is_file(),
                "cancelSupported": False, "nativeResumeSupported": False}

    def health(self):
        return {"processAlive": self.child is not None and self.child.poll() is None,
                "protocolReady": self.ready, "owner": self.owner,
                "protocolFault": self._protocol_fault, "credentials": "unverified"}

    def start(self):
        with self._mutex:
            if self._config_digest() != self._launch_digest:
                raise AdapterError("LAUNCH_CONFIG_CHANGED")
            if self._closed or not self.owner:
                raise AdapterError("NOT_ACTIVE_OWNER")
            if self.child:
                if self.ready:
                    return self.health()
                raise AdapterError("PROCESS_ALREADY_USED")
            with self._connect() as db:
                if self._receipt(db):
                    raise AdapterError("RECOVERY_READ_ONLY_NO_RESUBMIT")
            self.stderr = os.fdopen(self._private_file("runtime.stderr", append=True), "ab")
            self.child = subprocess.Popen(
                self.command, cwd=str(self.workspace), env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.stderr,
                start_new_session=True)
            self.reader = threading.Thread(target=self._read, daemon=True)
            self.reader.start()
        try:
            response = self._call("initialize", {"cwd": str(self.workspace),
                                  "provider": self.provider, "model": self.model})
            info = response.get("result", {}).get("serverInfo", {})
            if info.get("name") != "deepseek-harness-sdk-runtime":
                raise AdapterError("INITIALIZE_FAILED: " + _json(response))
            self.ready = True
            return self.health()
        except Exception:
            self.close()
            raise

    def _call(self, method, params=None):
        with self._mutex:
            if self._protocol_fault or not self.child or self.child.poll() is not None:
                raise AdapterError("PROTOCOL_UNAVAILABLE")
            self._counter += 1
            ident = self._counter
            waiter = queue.Queue()
            self._waiters[ident] = waiter
            request = {"jsonrpc": "2.0", "id": ident, "method": method}
            if params is not None:
                request["params"] = params
            try:
                self.child.stdin.write((_json(request) + "\n").encode())
                self.child.stdin.flush()
            except Exception:
                self._waiters.pop(ident, None)
                raise
        try:
            response = waiter.get(timeout=self.timeout)
            if isinstance(response, Exception):
                raise response
            return response
        finally:
            with self._mutex:
                self._waiters.pop(ident, None)

    def _unknown(self, reason):
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            receipt = self._receipt(db)
            if receipt and receipt["state"] != "failed":
                receipt.update(state="unknown", reason=reason, artifacts=[])
                self._save(db, receipt)

    def _fault(self, reason):
        self._protocol_fault, self.ready = reason, False
        self._unknown(reason)
        with self._mutex:
            for waiter in self._waiters.values():
                waiter.put(AdapterError(reason))

    def _read(self):
        try:
            while True:
                raw = self.child.stdout.readline(self.MAX_FRAME + 1)
                if not raw:
                    break
                if len(raw) > self.MAX_FRAME or not raw.endswith(b"\n"):
                    raise AdapterError("INVALID_FRAME_LENGTH")
                line = raw.decode("utf-8")
                with self._connect() as db:
                    db.execute("INSERT INTO frames(observed,raw) VALUES (?,?)", (time.time(), line))
                frame = json.loads(line)
                if not isinstance(frame, dict) or frame.get("jsonrpc") != "2.0":
                    raise AdapterError("INVALID_JSONRPC_FRAME")
                if "id" in frame:
                    if "method" in frame or ("result" in frame) == ("error" in frame):
                        raise AdapterError("INVALID_RESPONSE")
                    with self._mutex:
                        if frame["id"] in self._responded:
                            raise AdapterError("DUPLICATE_RESPONSE_ID")
                        waiter = self._waiters.get(frame["id"])
                        self._responded.add(frame["id"])
                    if waiter is None:
                        raise AdapterError("UNMATCHED_RESPONSE_ID")
                    waiter.put(frame)
                else:
                    self._event(frame)
        except Exception as error:
            self._fault("PROTOCOL_FAULT:" + str(error))
        finally:
            self.ready = False
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                receipt = self._receipt(db)
                if receipt and receipt["state"] not in self.TERMINAL:
                    receipt.update(state="unknown", reason="SDK_STREAM_CLOSED")
                    self._save(db, receipt)
            with self._mutex:
                for waiter in self._waiters.values():
                    waiter.put(AdapterError("SDK_STREAM_CLOSED"))

    def _event(self, frame):
        method, params = frame.get("method"), frame.get("params")
        if method not in {"session.event", "session.status", "subagent.started", "subagent.finished"} or not isinstance(params, dict):
            raise AdapterError("UNKNOWN_NOTIFICATION")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = self._receipt(db)
            if not r:
                return
            # Notifications cover all sessions, including subagents; they never
            # establish completion of our session by association alone.
            if params.get("sessionId") != r["sessionId"] or method != "session.event":
                return
            event = params.get("event")
            if not isinstance(event, dict) or type(event.get("seq")) is not int or event["seq"] < 0:
                raise AdapterError("INVALID_EVENT_SEQUENCE")
            old = db.execute("SELECT body FROM events WHERE seq=?", (event["seq"],)).fetchone()
            body = _json(event)
            if old:
                if old[0] != body:
                    raise AdapterError("CONFLICTING_EVENT_SEQUENCE")
                return
            if event["seq"] <= r["lastEventSeq"]:
                raise AdapterError("OUT_OF_ORDER_EVENT")
            db.execute("INSERT INTO events VALUES (?,?)", (event["seq"], body))
            r["lastEventSeq"], r["eventCount"] = event["seq"], r["eventCount"] + 1
            if r["state"] in {"unknown", "failed"}:
                self._save(db, r)
                return
            data = event.get("data")
            if not isinstance(data, dict):
                raise AdapterError("INVALID_EVENT_DATA")
            kind = event.get("type")
            if kind == "turn/start":
                if r["turn"] is not None or r["state"] == "awaiting_validation":
                    raise AdapterError("UNEXPECTED_SECOND_TURN")
                if type(data.get("turn")) is not int:
                    raise AdapterError("INVALID_TURN_ID")
                r.update(turn=data["turn"], state="running")
            elif kind == "turn/end":
                if r["turn"] is None or data.get("turn") != r["turn"] or r["endReason"] is not None:
                    raise AdapterError("UNMATCHED_TURN_END")
                reason = data.get("reason")
                if not isinstance(reason, dict):
                    raise AdapterError("INVALID_END_REASON")
                r["endReason"] = reason
                if reason.get("kind") == "error":
                    r.update(state="failed", reason=reason)
                elif reason.get("kind") != "completed":
                    r.update(state="unknown", reason=reason)
                elif r["messageId"]:
                    r.update(state="awaiting_validation", reason="RUNTIME_COMPLETED_NOT_VALIDATED")
            self._save(db, r)

    def submit(self, task_id, revision, attempt_id, prompt, instruction_digest):
        if not all(isinstance(v, str) and v for v in (task_id, attempt_id, prompt, instruction_digest)):
            raise ValueError("INVALID_CONTRACT")
        if type(revision) is not int or revision <= 0:
            raise ValueError("INVALID_REVISION")
        if self._config_digest() != self._launch_digest:
            raise AdapterError("LAUNCH_CONFIG_CHANGED")
        contract = _json({"taskId": task_id, "revision": revision, "attemptId": attempt_id,
                          "prompt": prompt, "instructionDigest": instruction_digest,
                          "workspace": str(self.workspace), "provider": self.provider, "model": self.model,
                          "launchDigest": self._launch_digest})
        with self._mutex:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                old = db.execute("SELECT id,contract,receipt FROM attempt WHERE slot=1").fetchone()
                if old:
                    if old[0] != attempt_id or old[1] != contract:
                        raise AdapterError("ATTEMPT_CONTRACT_CONFLICT")
                    return dict(json.loads(old[2]), deduplicated=True)
                if self._closed or not self.owner or not self.ready:
                    raise AdapterError("SDK_NOT_READY_OR_NOT_OWNER")
                receipt = {"taskId": task_id, "revision": revision, "attemptId": attempt_id,
                           "instructionDigest": instruction_digest, "workspace": str(self.workspace),
                           "sessionId": str(uuid.uuid4()), "messageId": None,
                           "state": "submitting", "reason": None, "accepted": False,
                           "lastEventSeq": -1, "eventCount": 0, "turn": None,
                           "endReason": None, "artifacts": [], "qaPassed": False,
                           "runtimeClosed": False, "submittedAt": time.time()}
                db.execute("INSERT INTO attempt VALUES (1,?,?,?)", (attempt_id, contract, _json(receipt)))
        threading.Thread(target=self._watch_deadline, daemon=True).start()
        try:
            response = self._call("session/prompt", {"sessionId": receipt["sessionId"],
                                  "contentBlocks": [{"type": "text", "text": prompt}]})
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                r = self._receipt(db)
                if "error" in response:
                    # A transport error is not proof the prompt was unexecuted.
                    r.update(state="unknown", reason={"rpcError": response["error"]})
                else:
                    mid = response.get("result", {}).get("messageId")
                    if not isinstance(mid, str) or not mid:
                        raise AdapterError("INVALID_MESSAGE_ID")
                    r.update(messageId=mid, accepted=True)
                    if r["state"] not in self.TERMINAL:
                        if r["endReason"] and r["endReason"].get("kind") == "completed":
                            r.update(state="awaiting_validation", reason="RUNTIME_COMPLETED_NOT_VALIDATED")
                        elif r["state"] == "submitting":
                            r["state"] = "accepted"
                self._save(db, r)
        except Exception as error:
            self._unknown("SUBMISSION_UNCERTAIN:" + type(error).__name__)
        return dict(self.get_status(attempt_id), deduplicated=False)

    def get_status(self, attempt_id):
        if not self.owner and not self._closed:
            # A observer can reconcile a dead owner but cannot resend its intent.
            try:
                fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                try:
                    with self._connect() as db:
                        db.execute("BEGIN IMMEDIATE")
                        r = self._receipt(db)
                        if r and (r["state"] not in self.TERMINAL or (r["state"] == "awaiting_validation" and not r.get("runtimeClosed"))):
                            r.update(state="unknown", reason="OWNER_LOST")
                            self._save(db, r)
                finally:
                    fcntl.flock(self._lock, fcntl.LOCK_UN)
        with self._connect() as db:
            r = self._receipt(db)
            if not r or r["attemptId"] != attempt_id:
                raise AdapterError("ATTEMPT_NOT_FOUND")
        return r

    def _watch_deadline(self):
        deadline = time.monotonic() + self.execution_timeout
        while not self._stopped.wait(min(.1, self.execution_timeout)):
            with self._connect() as db:
                r = self._receipt(db)
            if not r or r["state"] in self.TERMINAL:
                return
            if time.monotonic() >= deadline:
                self._unknown("EXECUTION_DEADLINE_EXCEEDED")
                if self.child and self.child.poll() is None:
                    try:
                        os.killpg(self.child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                return

    def _artifact(self, relative):
        path = Path(relative)
        if not isinstance(relative, str) or path.is_absolute() or not path.parts or any(x in {".", ".."} for x in path.parts):
            raise AdapterError("INVALID_ARTIFACT_PATH")
        descriptors = []
        try:
            fd = os.open(str(self.workspace), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            descriptors.append(fd)
            for part in path.parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                descriptors.append(fd)
            file_fd = os.open(path.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            descriptors.append(file_fd)
            before = os.fstat(file_fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise AdapterError("ARTIFACT_NOT_EXCLUSIVE_REGULAR_FILE")
            digest = hashlib.sha256()
            while True:
                data = os.read(file_fd, 1024 * 1024)
                if not data:
                    break
                digest.update(data)
            after = os.fstat(file_fd)
            named = os.stat(path.parts[-1], dir_fd=fd, follow_symlinks=False)
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if signature(before) != signature(after) or signature(after) != signature(named):
                raise AdapterError("ARTIFACT_CHANGED_DURING_COLLECTION")
            # Verify each directory is still named by the same parent: reject
            # replacements/renames between opening the chain and hashing.
            root_named = os.stat(str(self.workspace), follow_symlinks=False)
            root_fd = os.fstat(descriptors[0])
            if (root_named.st_dev, root_named.st_ino) != (root_fd.st_dev, root_fd.st_ino):
                raise AdapterError("WORKSPACE_CHANGED_DURING_COLLECTION")
            for index, part in enumerate(path.parts[:-1]):
                named_dir = os.stat(part, dir_fd=descriptors[index], follow_symlinks=False)
                opened_dir = os.fstat(descriptors[index + 1])
                if not stat.S_ISDIR(named_dir.st_mode) or (named_dir.st_dev, named_dir.st_ino) != (opened_dir.st_dev, opened_dir.st_ino):
                    raise AdapterError("ARTIFACT_DIRECTORY_CHANGED")
            return {"path": str(path), "sha256": digest.hexdigest(), "size": after.st_size}
        except OSError as error:
            raise AdapterError("UNSAFE_OR_MISSING_ARTIFACT:" + str(error)) from error
        finally:
            for fd in reversed(descriptors):
                os.close(fd)

    def collect(self, attempt_id, artifact_paths=()):
        r = self.get_status(attempt_id)
        if r["state"] != "awaiting_validation":
            if artifact_paths:
                raise AdapterError("ARTIFACTS_REQUIRE_RUNTIME_COMPLETION")
            return dict(r, artifacts=[])
        if not artifact_paths:
            raise AdapterError("ARTIFACTS_REQUIRED")
        if not r.get("runtimeClosed"):
            if not self.owner or self._closed:
                raise AdapterError("RUNTIME_QUIESCENCE_NOT_PROVEN")
            self.close()
            r = self.get_status(attempt_id)
            if r["state"] != "awaiting_validation" or not r.get("runtimeClosed"):
                raise AdapterError("RUNTIME_QUIESCENCE_NOT_PROVEN")
        artifacts = sorted([self._artifact(p) for p in artifact_paths], key=lambda a: a["path"])
        if len({a["path"] for a in artifacts}) != len(artifacts):
            raise AdapterError("DUPLICATE_ARTIFACT_PATH")
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            r = self._receipt(db)
            if r["state"] != "awaiting_validation":
                raise AdapterError("RUNTIME_STATE_CHANGED")
            if r["artifacts"] and r["artifacts"] != artifacts:
                raise AdapterError("ARTIFACT_MANIFEST_CHANGED")
            r["artifacts"] = artifacts
            self._save(db, r)
        return r

    def close(self):
        with self._close_mutex:
            self._close()

    def _close(self):
        if self._closed:
            return
        self._stopped.set()
        self._closing = True
        if self.child:
            if self.child.poll() is None:
                try:
                    self._call("shutdown")
                except Exception:
                    pass
                try:
                    self.child.wait(timeout=self.timeout)
                except subprocess.TimeoutExpired:
                    os.killpg(self.child.pid, signal.SIGKILL)
                    self.child.wait(timeout=3)
            # A SDK exit does not establish that its tool descendants exited.
            try:
                os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if self.reader:
                self.reader.join(timeout=3)
                if self.reader.is_alive():
                    self._unknown("SDK_READER_NOT_QUIESCENT")
            self.child.stdin.close()
            self.child.stdout.close()
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                r = self._receipt(db)
                if r:
                    r["runtimeClosed"] = not self.reader.is_alive()
                    self._save(db, r)
        if self.stderr:
            self.stderr.close()
        if self.owner:
            fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()
        self.ready, self._closed = False, True
