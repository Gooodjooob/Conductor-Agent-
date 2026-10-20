"""Claude Code --bare -p JSON adapter; no OAuth or host config inheritance.

Official protocol checked against CLI 2.1.168 and code.claude.com/docs/en/headless.
The durable one-attempt store and artifact checks reuse the P0 DSH transport.
Only CLI argv, not a shell command, is accepted from the trusted launcher.
"""
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from .dsh import DSHAdapter, AdapterError, _json


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("DUPLICATE_JSON_KEY")
        result[key] = value
    return result


class ClaudeAdapter(DSHAdapter):
    MAX_OUTPUT = 4 * 1024 * 1024

    def __init__(self, store_dir, command, workspace, env, timeout=10,
                 model="sonnet", execution_timeout=300):
        if not isinstance(model, str) or not model or len(model) > 160:
            raise ValueError("INVALID_CLAUDE_MODEL")
        super().__init__(store_dir, command, workspace, env, timeout,
                         provider="claude-cli", model=model,
                         execution_timeout=execution_timeout)

    def probe(self):
        return dict(super().probe(), transport="claude-print-json",
                    protocolStability="version-pinned-candidate")

    def start(self):
        with self._mutex:
            if self._closed or not self.owner:
                raise AdapterError("NOT_ACTIVE_OWNER")
            if self._config_digest() != self._launch_digest:
                raise AdapterError("LAUNCH_CONFIG_CHANGED")
            with self._connect() as db:
                if self._receipt(db):
                    raise AdapterError("RECOVERY_READ_ONLY_NO_RESUBMIT")
            self.ready = True
        return self.health()

    def submit(self, task_id, revision, attempt_id, prompt, instruction_digest):
        if not all(isinstance(v, str) and v for v in (task_id, attempt_id, prompt, instruction_digest)) or len(prompt.encode()) > 200000:
            raise ValueError("INVALID_CONTRACT")
        if type(revision) is not int or revision <= 0:
            raise ValueError("INVALID_REVISION")
        if self._config_digest() != self._launch_digest:
            raise AdapterError("LAUNCH_CONFIG_CHANGED")
        contract = _json({"taskId": task_id, "revision": revision,
                          "attemptId": attempt_id, "prompt": prompt,
                          "instructionDigest": instruction_digest,
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
                receipt = {"taskId": task_id, "revision": revision,
                           "attemptId": attempt_id, "instructionDigest": instruction_digest,
                           "workspace": str(self.workspace), "sessionId": str(uuid.uuid4()),
                           "messageId": None, "state": "submitting", "reason": None,
                           "accepted": False, "artifacts": [], "qaPassed": False,
                           "runtimeClosed": False, "submittedAt": time.time(),
                           "eventCount": 0, "lastEventSeq": -1}
                db.execute("INSERT INTO attempt VALUES (1,?,?,?)", (attempt_id, contract, _json(receipt)))
            # Intent is durable before process creation: any ambiguity is unknown.
            try:
                self.stderr = os.fdopen(self._private_file("runtime.stderr", append=True), "ab")
                flags = ["--bare", "--print", "--output-format", "json",
                         "--session-id", receipt["sessionId"], "--model", self.model,
                         "--setting-sources", "", "--strict-mcp-config",
                         "--mcp-config", '{"mcpServers":{}}',
                         "--permission-mode", "dontAsk",
                         "--tools", "Read,Write,Edit,Bash,Glob,Grep",
                         "--allowedTools", "Read,Write,Edit,Bash,Glob,Grep",
                         "--no-session-persistence"]
                self.child = subprocess.Popen(self.command + flags, cwd=str(self.workspace),
                    env=self.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=self.stderr, close_fds=True, start_new_session=True)
                with self._connect() as db:
                    r = self._receipt(db)
                    r.update(state="running", reason="CLI_STARTED_NOT_ACCEPTANCE_PROOF")
                    self._save(db, r)
                self.reader = threading.Thread(target=self._read_cli, args=(prompt,), daemon=True)
                self.reader.start()
                threading.Thread(target=self._watch_deadline, daemon=True).start()
            except Exception as error:
                self._unknown("CLAUDE_START_UNCERTAIN:" + type(error).__name__)
        return dict(self.get_status(attempt_id), deduplicated=False)

    def _read_cli(self, prompt):
        try:
            # Prompt stays off argv and never shares a stream with controller tokens.
            self.child.stdin.write(prompt.encode())
            self.child.stdin.close()
            raw = self.child.stdout.read(self.MAX_OUTPUT + 1)
            if len(raw) > self.MAX_OUTPUT:
                raise AdapterError("CLAUDE_OUTPUT_TOO_LARGE")
            code = self.child.wait(timeout=self.timeout)
            with self._connect() as db:
                db.execute("INSERT INTO frames(observed,raw) VALUES (?,?)", (time.time(), raw.decode("utf-8")))
            result = json.loads(raw, object_pairs_hook=strict_object)
            if not isinstance(result, dict) or result.get("type") != "result":
                raise AdapterError("CLAUDE_INVALID_RESULT")
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                r = self._receipt(db)
                if result.get("session_id") != r["sessionId"]:
                    raise AdapterError("CLAUDE_SESSION_MISMATCH")
                if r["state"] == "unknown":
                    return  # A late result cannot erase an execution timeout.
                mid = result.get("uuid")
                if not isinstance(mid, str):
                    raise AdapterError("CLAUDE_RESULT_UUID_REQUIRED")
                try:
                    uuid.UUID(mid)
                except ValueError as error:
                    raise AdapterError("CLAUDE_RESULT_UUID_INVALID") from error
                r.update(messageId=mid, accepted=True, eventCount=1,
                         resultSha256=hashlib.sha256(raw).hexdigest(), exitCode=code)
                if result.get("is_error") is True and result.get("subtype") in {
                    "error_during_execution", "error_max_turns", "error_max_budget_usd",
                    "error_max_structured_output_retries"}:
                    r.update(state="failed", reason="CLAUDE_" + result["subtype"])
                elif (code == 0 and result.get("is_error") is False and
                      result.get("subtype") == "success" and
                      isinstance(result.get("result"), str) and result["result"].strip() and
                      not result.get("permission_denials")):
                    r.update(state="awaiting_validation", reason="CLI_COMPLETED_NOT_VALIDATED")
                else:
                    r.update(state="unknown", reason="CLAUDE_RESULT_NOT_PROVEN_SUCCESS")
                self._save(db, r)
        except Exception as error:
            self._unknown("CLAUDE_EXECUTION_UNCERTAIN:" + type(error).__name__)
            if self.child and self.child.poll() is None:
                try:
                    os.killpg(self.child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        finally:
            self.ready = False

    def _close(self):
        if self._closed:
            return
        self._stopped.set()
        if self.child:
            if self.child.poll() is None:
                self._unknown("CLAUDE_STOPPED_WITHOUT_RESULT")
            try:
                os.killpg(self.child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            self.child.wait(timeout=3)
            if self.reader:
                self.reader.join(timeout=3)
                if self.reader.is_alive():
                    self._unknown("CLAUDE_READER_NOT_QUIESCENT")
            if not self.child.stdin.closed:
                self.child.stdin.close()
            self.child.stdout.close()
            with self._connect() as db:
                r = self._receipt(db)
                if r:
                    r["runtimeClosed"] = not self.reader or not self.reader.is_alive()
                    self._save(db, r)
        if self.stderr:
            self.stderr.close()
        if self.owner:
            import fcntl
            fcntl.flock(self._lock, fcntl.LOCK_UN)
        self._lock.close()
        self.ready, self._closed = False, True
