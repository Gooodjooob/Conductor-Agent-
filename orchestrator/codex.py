"""Codex CLI JSONL adapter for trusted, non-interactive development runs.

The adapter launches one ``codex exec`` process per attempt, sends the exact
prompt over stdin, and persists the event stream before reporting a result.
Only a successful ``turn.completed`` with an agent message reaches
``awaiting_validation``; process exit alone is never success.
"""
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import threading
import time
import uuid

from .dsh import AdapterError, _json


class CodexAdapter:
    MAX_LINE = 2 * 1024 * 1024
    MAX_OUTPUT = 16 * 1024 * 1024
    TERMINAL = {"failed", "unknown", "awaiting_validation"}

    def __init__(self, store_dir, command, workspace, env, timeout=10,
                 model=None, sandbox="workspace-write", execution_timeout=300):
        if not isinstance(command, (list, tuple)) or not command or not isinstance(command[0], str) or not Path(command[0]).is_absolute():
            raise ValueError("ABSOLUTE_CODEX_COMMAND_REQUIRED")
        if sandbox not in ("read-only", "workspace-write"):
            raise ValueError("CODEX_SANDBOX_MODE_NOT_ALLOWED")
        if not isinstance(env, dict) or "HOME" not in env or timeout <= 0 or execution_timeout <= 0:
            raise ValueError("EXPLICIT_ENV_HOME_AND_TIMEOUT_REQUIRED")
        self.store = Path(store_dir).absolute()
        self.workspace = Path(workspace).resolve(strict=True)
        if self.store == self.workspace or self.workspace in self.store.parents:
            raise ValueError("STORE_MUST_BE_OUTSIDE_WORKSPACE")
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        st = self.store.stat()
        if st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise AdapterError("STORE_MUST_BE_PRIVATE_OWNED_DIRECTORY")
        self.command = list(command); self.env = dict(env); self.model = model
        self.sandbox = sandbox; self.timeout = timeout; self.execution_timeout = execution_timeout
        self.launch_digest = self._config_digest()
        self.db_path = self.store / "codex.sqlite"
        self.lock_path = self.store / "owner.lock"
        self._mutex = threading.RLock(); self.child = None; self.reader = None
        self.owner = True; self.ready = False; self.closed = False
        self._initialize()

    def _config_digest(self):
        return hashlib.sha256(_json({"command": self.command, "env": self.env,
            "workspace": str(self.workspace), "model": self.model,
            "sandbox": self.sandbox, "timeout": self.timeout,
            "executionTimeout": self.execution_timeout}).encode()).hexdigest()

    def _db(self):
        db = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _initialize(self):
        with self._db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS config(slot INTEGER PRIMARY KEY CHECK(slot=1), digest TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS attempt(slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT UNIQUE, contract TEXT NOT NULL, receipt TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, raw TEXT NOT NULL);")
            row = db.execute("SELECT digest FROM config WHERE slot=1").fetchone()
            if row and row[0] != self.launch_digest: raise AdapterError("LAUNCH_CONFIG_CONFLICT")
            if not row: db.execute("INSERT INTO config VALUES(1,?)", (self.launch_digest,))
            old = db.execute("SELECT receipt FROM attempt WHERE slot=1").fetchone()
            if old:
                receipt = json.loads(old[0])
                if receipt.get("state") not in self.TERMINAL:
                    receipt.update(state="unknown", reason="OWNER_RESTARTED")
                    db.execute("UPDATE attempt SET receipt=? WHERE slot=1", (_json(receipt),))
        os.chmod(self.db_path, 0o600)

    def _receipt(self):
        with self._db() as db:
            row = db.execute("SELECT receipt FROM attempt WHERE slot=1").fetchone()
            return json.loads(row[0]) if row else None

    def _save(self, receipt):
        with self._db() as db:
            db.execute("UPDATE attempt SET receipt=? WHERE slot=1", (_json(receipt),))

    def probe(self):
        return {"transport":"codex-cli-jsonl", "protocolStability":"cli-jsonl",
                "executableExists":Path(self.command[0]).is_file(),
                "cancelSupported":True, "nativeResumeSupported":False,
                "sandbox":self.sandbox}

    def health(self):
        return {"processAlive":self.child is not None and self.child.poll() is None,
                "protocolReady":self.ready, "owner":self.owner}

    def start(self):
        if self.closed or not self.owner: raise AdapterError("NOT_ACTIVE_OWNER")
        if self._config_digest() != self.launch_digest: raise AdapterError("LAUNCH_CONFIG_CHANGED")
        if self._receipt(): raise AdapterError("RECOVERY_READ_ONLY_NO_RESUBMIT")
        self.ready = True
        return self.health()

    def submit(self, task_id, revision, attempt_id, prompt, instruction_digest):
        if type(revision) is not int or revision <= 0 or not all(isinstance(x, str) and x for x in (task_id, attempt_id, prompt, instruction_digest)):
            raise ValueError("INVALID_CONTRACT")
        if len(prompt.encode()) > 200000: raise ValueError("INVALID_PROMPT")
        with self._mutex:
            old = self._receipt()
            contract = _json({"taskId":task_id,"revision":revision,"attemptId":attempt_id,
                              "prompt":prompt,"instructionDigest":instruction_digest,
                              "launchDigest":self.launch_digest})
            if old:
                with self._db() as db:
                    row = db.execute("SELECT contract FROM attempt WHERE slot=1").fetchone()
                if row and row[0] == contract: return dict(old, deduplicated=True)
                raise AdapterError("ATTEMPT_CONTRACT_CONFLICT")
            if not self.ready: raise AdapterError("CODEX_NOT_READY")
            session_id = str(uuid.uuid4())
            receipt = {"taskId":task_id,"revision":revision,"attemptId":attempt_id,
                       "instructionDigest":instruction_digest,"sessionId":session_id,
                       "messageId":None,"state":"submitting","reason":None,
                       "accepted":False,"runtimeClosed":False,"eventCount":0,
                       "resultDigest":None,"submittedAt":time.time()}
            with self._db() as db:
                db.execute("INSERT INTO attempt VALUES(1,?,?,?)", (attempt_id, contract, _json(receipt)))
            args = self._command_args(prompt)
            try:
                self.child = subprocess.Popen(args, cwd=str(self.workspace), env=self.env,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    close_fds=True, start_new_session=True)
                receipt.update(state="running", reason="CODEX_PROCESS_STARTED_NOT_ACCEPTANCE_PROOF")
                self._save(receipt)
                self.reader = threading.Thread(target=self._read, args=(prompt,), daemon=True)
                self.reader.start()
                threading.Thread(target=self._watch, daemon=True).start()
            except Exception as exc:
                receipt.update(state="unknown", reason="CODEX_START_UNCERTAIN:"+type(exc).__name__)
                self._save(receipt)
        return dict(self._receipt(), deduplicated=False)

    def _command_args(self, prompt):
        args = list(self.command) + ["--ask-for-approval", "never", "--sandbox", self.sandbox,
                "exec", "--json", "--ephemeral", "--skip-git-repo-check", "--color", "never"]
        if self.model: args += ["--model", self.model]
        args += ["-"]
        return args

    def _read(self, prompt):
        try:
            self.child.stdin.write(prompt.encode()); self.child.stdin.close()
            total = 0; agent_message = None; completed = False; error = None
            for raw_line in iter(self.child.stdout.readline, b""):
                total += len(raw_line)
                if total > self.MAX_OUTPUT or len(raw_line) > self.MAX_LINE: raise AdapterError("CODEX_OUTPUT_TOO_LARGE")
                line = raw_line.decode("utf-8")
                with self._db() as db: db.execute("INSERT INTO events(raw) VALUES(?)", (line,))
                event = json.loads(line)
                if not isinstance(event, dict): raise AdapterError("CODEX_EVENT_NOT_OBJECT")
                kind = event.get("type")
                if kind == "result":
                    if event.get("is_error") is True:
                        error = event.get("result") or event.get("subtype") or "CODEX_RESULT_ERROR"
                    elif event.get("subtype") == "success" and isinstance(event.get("result"), str) and event["result"].strip():
                        agent_message = event["result"]; completed = True
                elif kind == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message" and isinstance(item.get("text"), str) and item["text"].strip():
                        agent_message = item["text"]
                    if item.get("type") == "error": error = item.get("message", "CODEX_ITEM_ERROR")
                elif kind == "turn.completed": completed = True
                elif kind in ("error", "turn.failed"): error = event.get("message", "CODEX_EVENT_ERROR")
            code = self.child.wait(timeout=self.timeout)
            receipt = self._receipt()
            if receipt.get("state") == "unknown": return
            receipt["eventCount"] = total
            receipt["exitCode"] = code
            if error or not completed or not agent_message or code != 0:
                receipt.update(state="failed" if code == 0 and error else "unknown",
                               reason=str(error or "CODEX_RESULT_NOT_PROVEN_SUCCESS"))
            else:
                receipt.update(state="awaiting_validation", accepted=True,
                    messageId=hashlib.sha256(agent_message.encode()).hexdigest(),
                    resultDigest=hashlib.sha256(agent_message.encode()).hexdigest(),
                    reason="CLI_COMPLETED_NOT_VALIDATED")
            self._save(receipt)
        except Exception as exc:
            receipt = self._receipt() or {}
            if receipt:
                receipt.update(state="unknown", reason="CODEX_EXECUTION_UNCERTAIN:"+type(exc).__name__)
                self._save(receipt)
        finally:
            self.ready = False
            for stream in (getattr(self.child, "stdout", None), getattr(self.child, "stderr", None)):
                if stream and not stream.closed:
                    stream.close()

    def _watch(self):
        deadline = time.monotonic() + self.execution_timeout
        while self.child and self.child.poll() is None and time.monotonic() < deadline: time.sleep(.1)
        if self.child and self.child.poll() is None:
            receipt = self._receipt() or {}; receipt.update(state="unknown", reason="CODEX_EXECUTION_TIMEOUT"); self._save(receipt)
            try: os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError: pass

    def collect(self, attempt_id, artifact_paths):
        receipt = self._receipt()
        if not receipt or receipt.get("attemptId") != attempt_id: raise AdapterError("ATTEMPT_NOT_FOUND")
        if receipt.get("state") != "awaiting_validation": raise AdapterError("RESULT_NOT_READY")
        artifacts = []
        for rel in artifact_paths:
            path = (self.workspace / rel).resolve()
            if self.workspace not in path.parents or path.is_symlink() or not path.is_file():
                raise AdapterError("UNSAFE_ARTIFACT")
            data = path.read_bytes()
            artifacts.append({'path': rel, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data)})
        artifacts.sort(key=lambda x:x['path'])
        receipt = dict(receipt, artifacts=artifacts, runtimeClosed=True)
        self._save(receipt)
        return receipt

    def get_status(self, attempt_id):
        receipt = self._receipt()
        if not receipt or receipt.get("attemptId") != attempt_id: raise AdapterError("ATTEMPT_NOT_FOUND")
        return dict(receipt)

    def close(self):
        if self.closed: return
        self.closed = True; self.ready = False
        if self.child and self.child.poll() is None:
            receipt = self._receipt() or {}
            if receipt.get("state") not in self.TERMINAL:
                receipt.update(state="unknown", reason="CODEX_STOPPED_WITHOUT_RESULT"); self._save(receipt)
            try: os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError: pass
        if self.reader: self.reader.join(timeout=3)
        if self.child and self.child.stdin and not self.child.stdin.closed: self.child.stdin.close()
        if self.child:
            for stream in (self.child.stdout, self.child.stderr):
                if stream and not stream.closed: stream.close()
