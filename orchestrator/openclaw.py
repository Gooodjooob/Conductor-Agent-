"""OpenClaw Gateway adapter shared by QClaw/ClawHive-compatible runtimes.

This is deliberately runtime-oriented.  It does not claim that a product such
as QClaw or ClawHive exposes an official public API.  The trusted side calls a
documented/explicit OpenClaw CLI bridge, persists the idempotent attempt, and
keeps accepted/unknown/terminal states fail-closed.
"""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import time
import uuid

from .dsh import AdapterError, _json


class OpenClawError(AdapterError):
    pass


_READ_ONLY_MODES = {"all", "non-main", "container"}
_READ_ONLY_WORKSPACE_ACCESS = {"ro", "none"}
_REQUIRED_DENIED_TOOLS = {"exec", "process", "network"}


def _effective_value(agent, defaults, key):
    """Resolve an agent setting without retaining the upstream config."""
    if isinstance(agent, dict) and key in agent:
        return agent[key]
    if isinstance(defaults, dict):
        return defaults.get(key)
    return None


def _tool_is_disabled(agent, defaults, name):
    """Return whether a potentially dangerous capability is explicitly off."""
    for scope in (defaults, agent):
        if not isinstance(scope, dict):
            continue
        tools = scope.get("tools")
        if not isinstance(tools, dict):
            tools = {}
        deny = tools.get("deny")
        if isinstance(deny, (list, tuple, set)) and name in {str(item).lower() for item in deny}:
            return True
        if tools.get(name) is False or scope.get(name) is False:
            return True
        setting = tools.get(name)
        if isinstance(setting, dict) and str(setting.get("mode", "")).lower() in {"deny", "disabled", "off"}:
            return True
        if isinstance(setting, dict) and setting.get("enabled") is False:
            return True
        allow = tools.get("allow")
        if isinstance(allow, (list, tuple, set)) and name not in {str(item).lower() for item in allow}:
            return True
        sandbox = scope.get("sandbox")
        if name == "network" and isinstance(sandbox, dict) and sandbox.get("network") is False:
            return True
        if name == "network" and isinstance(sandbox, dict):
            docker = sandbox.get("docker")
            if isinstance(docker, dict) and str(docker.get("network", "")).lower() in {"none", "disabled", "off"}:
                return True
    return False


def read_only_policy(config_result, agent_id, workspace):
    """Derive a minimal, non-sensitive read-only policy from config.get.

    This deliberately requires explicit evidence for every dangerous capability
    and for the agent workspace. A prompt saying "do not write" is not policy.
    """
    config = config_result.get("config", config_result) if isinstance(config_result, dict) else {}
    if not isinstance(config, dict):
        return {"safe": False, "reason": "config_invalid"}
    agents_config = config.get("agents")
    if not isinstance(agents_config, dict):
        return {"safe": False, "reason": "agents_config_missing"}
    defaults = agents_config.get("defaults") if isinstance(agents_config.get("defaults"), dict) else {}
    agents = agents_config.get("list")
    if not isinstance(agents, list):
        return {"safe": False, "reason": "agent_list_missing"}
    agent = next((item for item in agents if isinstance(item, dict) and item.get("id") == agent_id), None)
    if agent is None:
        return {"safe": False, "reason": "agent_not_found"}
    sandbox = agent.get("sandbox") if isinstance(agent.get("sandbox"), dict) else {}
    default_sandbox = defaults.get("sandbox") if isinstance(defaults.get("sandbox"), dict) else {}
    mode = sandbox.get("mode", default_sandbox.get("mode"))
    workspace_access = sandbox.get("workspaceAccess", default_sandbox.get("workspaceAccess"))
    if mode not in _READ_ONLY_MODES or workspace_access not in _READ_ONLY_WORKSPACE_ACCESS:
        return {"safe": False, "reason": "sandbox_not_read_only", "mode": mode, "workspaceAccess": workspace_access}
    missing = sorted(name for name in _REQUIRED_DENIED_TOOLS if not _tool_is_disabled(agent, defaults, name))
    if missing:
        return {"safe": False, "reason": "dangerous_tools_not_disabled", "missing": missing,
                "mode": mode, "workspaceAccess": workspace_access}
    declared_workspace = None
    for key in ("workspace", "workspaceDir", "workspacePath"):
        value = agent.get(key) if isinstance(agent, dict) else None
        if isinstance(value, str) and value:
            declared_workspace = value
            break
    if declared_workspace is None:
        return {"safe": False, "reason": "agent_workspace_not_declared",
                "mode": mode, "workspaceAccess": workspace_access}
    try:
        workspace_match = Path(declared_workspace).expanduser().resolve() == Path(workspace).resolve()
    except (OSError, RuntimeError):
        workspace_match = False
    if not workspace_match:
        return {"safe": False, "reason": "agent_workspace_mismatch",
                "mode": mode, "workspaceAccess": workspace_access}
    return {"safe": True, "reason": "explicit_read_only_policy", "mode": mode,
            "workspaceAccess": workspace_access, "workspaceMatch": True}


def _latest_assistant_text(history):
    messages = history.get("messages", []) if isinstance(history, dict) else []
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        if isinstance(message.get("text"), str) and message["text"].strip():
            return message["text"].strip()
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            text = "".join(
                item.get("text", "") for item in content
                if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
            ).strip()
            if text:
                return text
    return None


class OpenClawGatewayTransport:
    """Safe argv transport for ``openclaw gateway call``.

    ``call_impl`` is accepted for fixture tests and for a future authenticated
    desktop bridge.  Real invocations never use a shell and never put tokens in
    argv; the OpenClaw state/config paths are passed through the environment.
    """

    def __init__(self, command=None, state_dir=None, config_path=None, cwd=None,
                 env=None, timeout_grace=3000, call_impl=None):
        if call_impl is None:
            if not isinstance(command, (list, tuple)) or not command or not isinstance(command[0], str):
                raise ValueError("ABSOLUTE_OPENCLAW_COMMAND_REQUIRED")
            if not Path(command[0]).is_absolute():
                raise ValueError("ABSOLUTE_OPENCLAW_COMMAND_REQUIRED")
            if not isinstance(state_dir, str) or not Path(state_dir).is_absolute():
                raise ValueError("ABSOLUTE_OPENCLAW_STATE_DIR_REQUIRED")
        self.command = list(command or [])
        self.state_dir = str(Path(state_dir).resolve()) if state_dir else None
        self.config_path = str(Path(config_path).resolve()) if config_path else None
        self.cwd = str(Path(cwd or os.getcwd()).resolve())
        self.env = dict(env or {})
        self.timeout_grace = timeout_grace
        self.call_impl = call_impl

    def probe(self):
        return {
            "transport": "openclaw-gateway-cli",
            "protocolStability": "gateway-rpc",
            "executableExists": bool(self.command and Path(self.command[0]).is_file()),
            "stateDirConfigured": bool(self.state_dir),
            "configPathConfigured": bool(self.config_path),
            "cancelSupported": True,
            "nativeResumeSupported": True,
        }

    @staticmethod
    def _parse(stdout):
        lines = [line for line in str(stdout or "").splitlines()
                 if not line.startswith("[proxy-bootstrap]")]
        text = "\n".join(lines).strip()
        if not text:
            raise OpenClawError("OPENCLAW_EMPTY_RESPONSE")
        starts = [0] + [i for i, line in enumerate(lines[1:], 1) if line.lstrip().startswith("{")]
        last = None
        for start in starts:
            try:
                value = json.loads("\n".join(lines[start:]).strip())
                if not isinstance(value, dict):
                    raise ValueError("OPENCLAW_RESPONSE_MUST_BE_OBJECT")
                return value
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                last = exc
        raise OpenClawError("OPENCLAW_INVALID_JSON") from last

    def call(self, method, params, timeout_ms):
        if self.call_impl is not None:
            return self.call_impl(method, params, timeout_ms)
        env = dict(os.environ)
        env.update(self.env)
        env["OPENCLAW_STATE_DIR"] = self.state_dir
        if self.config_path:
            env["OPENCLAW_CONFIG_PATH"] = self.config_path
        args = self.command + ["gateway", "call", method, "--params", _json(params),
                               "--json", "--timeout", str(timeout_ms)]
        try:
            result = subprocess.run(args, cwd=self.cwd, env=env, shell=False,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True,
                                    timeout=(timeout_ms + self.timeout_grace) / 1000)
        except subprocess.TimeoutExpired as exc:
            raise OpenClawError("OPENCLAW_GATEWAY_TIMEOUT") from exc
        except OSError as exc:
            raise OpenClawError("OPENCLAW_GATEWAY_UNREACHABLE") from exc
        if result.returncode != 0:
            raise OpenClawError("OPENCLAW_GATEWAY_CALL_FAILED")
        return self._parse(result.stdout)


class OpenClawAdapter:
    """Durable one-attempt OpenClaw Gateway lifecycle adapter."""

    TERMINAL = {"failed", "unknown", "awaiting_validation", "cancelled", "timed_out"}

    def __init__(self, store_dir, workspace, transport, agent_id,
                 session_key=None, timeout=30, execution_timeout=300,
                 model=None, require_read_only_policy=False):
        if not isinstance(agent_id, str) or not agent_id:
            raise ValueError("OPENCLAW_AGENT_ID_REQUIRED")
        self.store = Path(store_dir).absolute()
        self.workspace = Path(workspace).resolve(strict=True)
        if self.store == self.workspace or self.workspace in self.store.parents:
            raise ValueError("STORE_MUST_BE_OUTSIDE_WORKSPACE")
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.store.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise OpenClawError("STORE_MUST_BE_PRIVATE_OWNED_DIRECTORY")
        self.transport = transport
        self.agent_id = agent_id
        self.session_key = session_key or f"agent:{agent_id}:orchestrator"
        self.timeout = timeout
        self.execution_timeout = execution_timeout
        self.model = model
        self.require_read_only_policy = require_read_only_policy is True
        self.preflight = None
        self.db_path = self.store / "openclaw.sqlite"
        self.launch_digest = hashlib.sha256(_json({
            "workspace": str(self.workspace), "agentId": agent_id,
            "sessionKey": self.session_key, "model": model,
            "requireReadOnlyPolicy": self.require_read_only_policy,
            "transport": transport.probe(), "timeout": timeout,
            "executionTimeout": execution_timeout,
        }).encode()).hexdigest()
        self.ready = False
        self.closed = False
        self._initialize()

    def _db(self):
        db = sqlite3.connect(str(self.db_path), timeout=10, isolation_level=None)
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _initialize(self):
        with self._db() as db:
            db.executescript("CREATE TABLE IF NOT EXISTS config(slot INTEGER PRIMARY KEY CHECK(slot=1), digest TEXT NOT NULL);"
                             "CREATE TABLE IF NOT EXISTS attempt(slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT UNIQUE, contract TEXT NOT NULL, receipt TEXT NOT NULL);")
            row = db.execute("SELECT digest FROM config WHERE slot=1").fetchone()
            if row and row[0] != self.launch_digest:
                raise OpenClawError("LAUNCH_CONFIG_CONFLICT")
            if not row:
                db.execute("INSERT INTO config VALUES(1,?)", (self.launch_digest,))
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
        result = self.transport.probe()
        result.update(host="openclaw", agentId=self.agent_id, sessionKey=self.session_key,
                      model=self.model, nativeResumeSupported=True)
        return result

    def health(self):
        return {"protocolReady": self.ready, "closed": self.closed,
                "transport": self.transport.probe()["transport"],
                "readOnlyPreflight": self.preflight}

    def _run_preflight(self):
        if not self.require_read_only_policy:
            return {"safe": True, "reason": "not_required"}
        try:
            config = self.transport.call("config.get", {}, 10000)
        except OpenClawError as exc:
            raise OpenClawError("OPENCLAW_READ_ONLY_PREFLIGHT_FAILED") from exc
        policy = read_only_policy(config, self.agent_id, self.workspace)
        if not policy.get("safe"):
            raise OpenClawError("OPENCLAW_READ_ONLY_POLICY_REQUIRED")
        return policy

    def start(self):
        if self.closed:
            raise OpenClawError("NOT_ACTIVE_OWNER")
        if self._receipt():
            raise OpenClawError("RECOVERY_READ_ONLY_NO_RESUBMIT")
        self.preflight = self._run_preflight()
        self.ready = True
        return self.health()

    def _recover(self, attempt_id):
        ledger = self.transport.call("tasks.list", {"sessionKey": self.session_key, "limit": 50}, 10000)
        tasks = ledger.get("tasks", []) if isinstance(ledger, dict) else []
        return next((task for task in tasks if isinstance(task, dict) and task.get("runId") == attempt_id), None)

    def _cancel_exact(self, attempt_id):
        task = self._recover(attempt_id)
        if not task:
            return {"confirmed": False, "reason": "task_not_found"}
        if task.get("status") in {"completed", "failed", "cancelled", "timed_out"}:
            return {"confirmed": True, "terminal": task["status"]}
        task_id = task.get("taskId") or task.get("id")
        if not task_id:
            return {"confirmed": False, "reason": "task_id_missing"}
        result = self.transport.call("tasks.cancel", {"taskId": task_id, "reason": "orchestrator timeout"}, 10000)
        if isinstance(result, dict) and result.get("cancelled") is True:
            return {"confirmed": True, "terminal": "cancelled"}
        refreshed = self._recover(attempt_id)
        if refreshed and refreshed.get("status") in {"completed", "failed", "cancelled", "timed_out"}:
            return {"confirmed": True, "terminal": refreshed["status"]}
        return {"confirmed": False, "reason": "cancel_unconfirmed"}

    def submit(self, task_id, revision, attempt_id, prompt, instruction_digest):
        if not self.ready:
            raise OpenClawError("OPENCLAW_NOT_READY")
        if type(revision) is not int or revision <= 0 or not all(isinstance(x, str) and x for x in (task_id, attempt_id, prompt, instruction_digest)):
            raise ValueError("INVALID_CONTRACT")
        contract = _json({"taskId": task_id, "revision": revision, "attemptId": attempt_id,
                          "prompt": prompt, "instructionDigest": instruction_digest,
                          "agentId": self.agent_id, "sessionKey": self.session_key,
                          "launchDigest": self.launch_digest})
        old = self._receipt()
        if old:
            with self._db() as db:
                row = db.execute("SELECT contract FROM attempt WHERE slot=1").fetchone()
            if row and row[0] == contract:
                return dict(old, deduplicated=True)
            raise OpenClawError("ATTEMPT_CONTRACT_CONFLICT")
        # Gateway receipt sessionId is a local contract identifier.  It must
        # remain a strict identifier; the OpenClaw runtime sessionKey is kept
        # in the frozen contract and transport params and may contain ':'.
        receipt = {"taskId": task_id, "revision": revision, "attemptId": attempt_id,
                   "instructionDigest": instruction_digest,
                   "sessionId": uuid.uuid4().hex,
                   "messageId": None, "state": "submitting", "reason": None,
                   "accepted": False, "runtimeClosed": False, "artifacts": [],
                   "resultDigest": None, "submittedAt": time.time()}
        with self._db() as db:
            db.execute("INSERT INTO attempt VALUES(1,?,?,?)", (attempt_id, contract, _json(receipt)))
        try:
            accepted = self.transport.call("agent", {
                "message": prompt, "agentId": self.agent_id, "sessionKey": self.session_key,
                "timeout": self.timeout, "deliver": False, "idempotencyKey": attempt_id,
                "label": task_id, **({"model": self.model} if self.model else {}),
            }, 30000)
        except OpenClawError:
            accepted = None
            try:
                if self._recover(attempt_id):
                    accepted = {"runId": attempt_id, "status": "in_flight"}
            except OpenClawError:
                pass
            if not accepted:
                receipt.update(state="unknown", reason="OPENCLAW_START_STATE_UNKNOWN")
                self._save(receipt)
                return dict(receipt, deduplicated=False)
        if not isinstance(accepted, dict) or accepted.get("runId") != attempt_id or accepted.get("status") not in {"accepted", "in_flight"}:
            receipt.update(state="failed", reason="OPENCLAW_INVALID_ACCEPT_RESPONSE")
            self._save(receipt)
            return dict(receipt, deduplicated=False)
        receipt.update(state="accepted", accepted=True)
        self._save(receipt)
        try:
            waited = self.transport.call("agent.wait", {"runId": attempt_id, "timeoutMs": self.execution_timeout * 1000}, self.execution_timeout * 1000 + 3000)
            status = waited.get("status") if isinstance(waited, dict) else None
            if status in {"timeout", "pending"}:
                cancellation = self._cancel_exact(attempt_id)
                receipt.update(state="timed_out" if cancellation.get("confirmed") else "unknown",
                               reason="OPENCLAW_TIMEOUT" if cancellation.get("confirmed") else "OPENCLAW_TIMEOUT_CANCEL_UNCONFIRMED")
            elif status == "error":
                receipt.update(state="failed", reason="OPENCLAW_AGENT_FAILED")
            elif status != "ok":
                receipt.update(state="unknown", reason="OPENCLAW_WAIT_STATE_UNKNOWN")
            else:
                history = self.transport.call("chat.history", {"sessionKey": self.session_key, "agentId": self.agent_id, "limit": 50}, 15000)
                text = _latest_assistant_text(history)
                if not text:
                    receipt.update(state="failed", reason="OPENCLAW_RESULT_MISSING")
                else:
                    digest = hashlib.sha256(text.encode()).hexdigest()
                    receipt.update(state="awaiting_validation", runtimeClosed=True,
                                   messageId=digest, resultDigest=digest,
                                   reason="OPENCLAW_COMPLETED_NOT_VALIDATED")
        except OpenClawError as exc:
            receipt.update(state="unknown", reason=str(exc))
        self._save(receipt)
        return dict(receipt, deduplicated=False)

    def get_status(self, attempt_id):
        receipt = self._receipt()
        if not receipt or receipt.get("attemptId") != attempt_id:
            raise OpenClawError("ATTEMPT_NOT_FOUND")
        return dict(receipt)

    def cancel(self, attempt_id):
        receipt = self.get_status(attempt_id)
        if receipt["state"] in self.TERMINAL:
            return receipt
        result = self._cancel_exact(attempt_id)
        receipt.update(state="cancelled" if result.get("confirmed") else "unknown",
                       reason="OPENCLAW_CANCELLED" if result.get("confirmed") else "OPENCLAW_CANCEL_UNCONFIRMED")
        self._save(receipt)
        return receipt

    def collect(self, attempt_id, artifact_paths):
        receipt = self.get_status(attempt_id)
        if receipt.get("state") != "awaiting_validation":
            raise OpenClawError("RESULT_NOT_READY")
        artifacts = []
        for relative in artifact_paths:
            path = (self.workspace / relative).resolve()
            if self.workspace not in path.parents or path.is_symlink() or not path.is_file():
                raise OpenClawError("UNSAFE_ARTIFACT")
            data = path.read_bytes()
            artifacts.append({"path": relative, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)})
        receipt.update(artifacts=sorted(artifacts, key=lambda item: item["path"]))
        self._save(receipt)
        return receipt

    def close(self):
        self.closed = True
        self.ready = False
