"""WorkBuddy / CodeBuddy CLI 宿主适配器 —— 任务卡 D4-WB-BRIDGE-001 §4.2。

职责：把 D4 任务提交给本机 WorkBuddy 内置的 ``codebuddy`` CLI，并把**真实回执**回传 D4。
本适配器不产生"完成"判定：最多报告"宿主已接收"与"CLI 会话已正常结束"；
交付是否成立由产物摘要（size + SHA-256 + 路径校验）与独立验收决定。

边界（对齐 §4.1 / §4.2 / §9）：

- 只以 **argv 数组** 调用宿主，``shell=False``；绝不拼接 shell 字符串。
- 固定消息复用 D4 投递消息合同（goal / workspace / permissions / acceptance / attemptId）。
- ``stream-json`` 逐行解析；原始事件始终落盘，另存脱敏摘要；二者都不进 argv。
- 状态区分 ``submitted`` / ``accepted_by_host`` / ``started`` / ``completed`` / ``unknown``。
- **只有** ``system/init`` 的 ``session_id`` 与本次请求值精确一致，才记为 ``accepted_by_host``。
- 进程退出码、``result`` 事件、``subtype=success`` 都**不等于**交付成功——实测这三者可与
  "什么都没做"同时出现，详见 ``research-gate.md`` §3.4。
- 异常、断线、非 JSON 行、协议不符、超时一律 fail closed；未知结果不自动重试。
- 同一 attempt 重复提交只回放第一次结果；返工必须由 D4 创建新 attempt。
- 适配器不把模型文本写进 D4 可信状态字段，也不直接调用 D4 服务层。
- 本机实测宿主 CLI **不提供** workspace 路径隔离（``acceptEdits`` 下 cwd 外写入失败与否
  取决于宿主）。因此适配器只保证**声明层**校验：产物收集时拒绝 symlink、非 regular file、
  realpath 逃逸出 workspace 的条目。真实文件系统边界的补齐需 cox 另行裁定（见能力报告）。
"""
from __future__ import annotations

import hashlib
import csv
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time

from adapters.base import AdapterCapabilities, AdapterResult, HostAdapter
from adapters.qclaw_gateway import build_task_message
from contracts.schemas import ContractError
from contracts.document_io import parse_frontmatter
from adapters.task_permissions import MEMBERS, build_policy, cli_arguments

# 受控来源 id：由 d4ctl 固定注入，不接受调用者自选。
WORKBUDDY_SOURCE_ID = "workbuddy-codebuddy"
DEFAULT_WORKBUDDY_MODEL = "deepseek-v4.1-flash"
ALLOWED_WORKBUDDY_MODELS = (
    DEFAULT_WORKBUDDY_MODEL, "hy4-preview", "glm-5.3-flash",
    "custom-local:glm-5.3-flash",
)
WORKBUDDY_AGENT_ID = "dev-wkb1"
WORKBUDDY_AGENT_IDS = ("dev-wkb1", "qa-wkb1")
WORKBUDDY_PLUGIN_ROOT = Path("/Users/demo/.workbuddy/plugins/marketplaces/my-experts/plugins/dev-wkb1")
WORKBUDDY_REGISTRY = Path(__file__).resolve().parents[3] / "docs/development/agent-registry.md"


def workbuddy_binding(agent_id):
    """Read a registered dev/QA expert; no arbitrary plugin/agent override."""
    if agent_id not in WORKBUDDY_AGENT_IDS:
        raise ValueError("WORKBUDDY_MEMBER_NOT_BOUND:%s" % agent_id)
    rows = [[cell.strip() for cell in row] for row in csv.reader(
        WORKBUDDY_REGISTRY.read_text(encoding="utf-8").splitlines(), delimiter="|")]
    members = [row for row in rows if len(row) == 9 and row[1] == agent_id]
    if len(members) != 1 or "WorkBuddy" not in members[0][2] or not members[0][7].startswith("active"):
        raise ValueError("WORKBUDDY_MEMBER_NOT_ACTIVE_OR_AMBIGUOUS")
    root = (WORKBUDDY_PLUGIN_ROOT if agent_id == WORKBUDDY_AGENT_ID else
            WORKBUDDY_PLUGIN_ROOT.parent / agent_id).resolve(strict=True)
    manifest_path = root / ".codebuddy-plugin/plugin.json"
    definition_path = root / ("agents/%s.md" % agent_id)
    for path in (manifest_path, definition_path):
        if path.is_symlink() or not path.is_file() or root not in path.resolve().parents:
            raise ValueError("WORKBUDDY_EXPERT_FILE_INVALID")
    manifest_bytes = manifest_path.read_bytes()
    definition = definition_path.read_bytes()
    frontmatter, body = parse_frontmatter(definition.decode("utf-8"))
    manifest = json.loads(manifest_bytes)
    if (manifest.get("name") != agent_id or manifest.get("agentName") != agent_id
            or manifest.get("agents") != ["./agents/%s.md" % agent_id]
            or manifest.get("expertType") != "agent"):
        raise ValueError("WORKBUDDY_EXPERT_MANIFEST_MISMATCH")
    denied = "Edit,WebFetch,WebSearch" if agent_id.startswith("qa-") else "WebFetch,WebSearch"
    turns = MEMBERS[agent_id][2]
    if (frontmatter.get("name") != agent_id or not body.strip()
            or frontmatter.get("disallowedTools") != denied
            or frontmatter.get("maxTurns") != turns):
        raise ValueError("WORKBUDDY_EXPERT_POLICY_MISMATCH")
    return {"agentId": agent_id, "hostAgent": agent_id, "pluginDir": str(root),
            "definitionPath": str(definition_path), "profileMode": "frozen-expert-instructions",
            "disallowedTools": denied.split(","), "maxTurns": turns,
            "manifestSha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "definitionSha256": hashlib.sha256(definition).hexdigest(),
            "registryStatus": members[0][7],
            "registryRowSha256": _sha256_text(_json(members[0])),
            "evidenceLevel": "LOCAL", "runtimeIdentity": "UNVERIFIED"}

# research-gate §3.3：唯一能让宿主真实写文件的权限模式。
# ``dontAsk`` / ``default`` 下 Write/Bash 全被拒；``bypassPermissions`` 属 §9 禁止项。
DEFAULT_PERMISSION_MODE = "acceptEdits"
ALLOWED_PERMISSION_MODES = ("acceptEdits",)

# 适配器自报的宿主状态（与 D4 attempt.state 解耦，仅用于回执说明）。
HOST_STATES = ("submitted", "accepted_by_host", "started", "completed", "unknown")

DENIED_PREFIX = "Error: Permission to use"


class WorkBuddyTransportError(RuntimeError):
    """transport 层错误（启动失败/超时/协议不符）。

    调用方 fail closed：记 submit_unknown / 不产生交付，不自动重试。
    """


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def parse_stream_line(raw_line):
    """解析一行 stream-json；返回事件对象。

    协议损坏（非 JSON / 非对象）一律抛错，不静默跳过——静默跳过会把"空回执"
    误当成"没有坏消息"，正是 §6 要求 fail closed 的场景。
    """
    line = raw_line.strip()
    if not line:
        raise WorkBuddyTransportError("WORKBUDDY_EMPTY_LINE")
    try:
        event = json.loads(line)
    except ValueError as exc:
        raise WorkBuddyTransportError("WORKBUDDY_INVALID_JSON") from exc
    if not isinstance(event, dict):
        raise WorkBuddyTransportError("WORKBUDDY_EVENT_NOT_OBJECT")
    if not isinstance(event.get("type"), str) or not event["type"]:
        raise WorkBuddyTransportError("WORKBUDDY_EVENT_TYPE_MISSING")
    return event


def safe_event(event):
    """脱敏事件：只保留结构与判定所需的字段，不保留完整模型文本。

    原始事件另存 ``events.jsonl``；本函数产出的是可进报告/摘要的安全视图。
    """
    kind = event.get("type")
    safe = {"type": kind}
    if kind == "system":
        safe["subtype"] = event.get("subtype")
        if event.get("subtype") == "init":
            tools = event.get("tools")
            safe["sessionId"] = event.get("session_id")
            safe["model"] = event.get("model")
            safe["permissionMode"] = event.get("permissionMode")
            safe["toolCount"] = len(tools) if isinstance(tools, list) else None
        return safe
    if kind == "result":
        result_text = event.get("result")
        safe["subtype"] = event.get("subtype")
        safe["isError"] = event.get("is_error")
        safe["resultDigest"] = _sha256_text(result_text) if isinstance(result_text, str) else None
        safe["resultLength"] = len(result_text) if isinstance(result_text, str) else None
        return safe
    if kind in ("assistant", "user"):
        message = event.get("message")
        blocks = message.get("content") if isinstance(message, dict) else None
        tools_used = []
        tools_denied = 0
        texts = 0
        for block in blocks if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                tools_used.append(str(block.get("name")))
            elif block.get("type") == "tool_result":
                body = block.get("content")
                if isinstance(body, list):
                    body = " ".join(
                        str(item.get("text") if isinstance(item, dict) else item) for item in body
                    )
                if isinstance(body, str) and body.startswith(DENIED_PREFIX):
                    tools_denied += 1
            elif block.get("type") == "text":
                texts += 1
        if tools_used:
            # 工具名是结构信息，保留；工具入参可能含路径或业务内容，不保留。
            safe["toolUse"] = sorted(set(tools_used))
        if tools_denied:
            safe["toolDeniedCount"] = tools_denied
        if texts:
            safe["textBlockCount"] = texts
        return safe
    return safe


class CodeBuddyTransport:
    """一次性受控 CLI 调用。

    - ``command[0]`` 必须是绝对路径的可执行文件；``cwd`` 即 workspace（已 realpath）。
    - ``shell=False``；prompt 以 argv 位置参数传入，不经 shell 解释。
    - 事件流逐行读入并落盘；超出上限或协议损坏立即中止并 fail closed。
    """

    evidence_level = "LOCAL"

    def __init__(self, command, workspace, store_dir,
                 permission_mode=DEFAULT_PERMISSION_MODE, model=None,
                 timeout=600.0, max_output=64 * 1024 * 1024, max_line=4 * 1024 * 1024,
                 env=None, persist_session=True, binding=None):
        if not isinstance(command, (list, tuple)) or not command:
            raise ValueError("WORKBUDDY_COMMAND_REQUIRED")
        if not isinstance(command[0], str) or not Path(command[0]).is_absolute():
            raise ValueError("WORKBUDDY_COMMAND_MUST_BE_ABSOLUTE")
        if permission_mode not in ALLOWED_PERMISSION_MODES:
            raise ValueError("WORKBUDDY_PERMISSION_MODE_NOT_ALLOWED")
        self.command = list(command)
        self.workspace = Path(workspace).resolve(strict=True)
        self.store = Path(store_dir)
        self.permission_mode = permission_mode
        self.model = model
        if model is not None and model not in ALLOWED_WORKBUDDY_MODELS:
            raise ValueError("WORKBUDDY_MODEL_NOT_ALLOWED")
        self.binding = dict(binding) if binding else None
        self.timeout = float(timeout)
        self.max_output = int(max_output)
        self.max_line = int(max_line)
        self.env = dict(env or {})
        if not isinstance(persist_session, bool):
            raise ValueError("WORKBUDDY_PERSIST_SESSION_MUST_BE_BOOL")
        self.persist_session = persist_session
        self.task_policy = None
        if "HOME" not in self.env:
            raise ValueError("WORKBUDDY_EXPLICIT_ENV_HOME_REQUIRED")

    def argv(self, session_id, prompt):
        """argv 数组：宿主参数在前，prompt 作为最后一个位置参数。"""
        args = list(self.command) + [
            "--print",
            "--output-format", "stream-json",
            "--permission-mode", self.permission_mode,
            "--session-id", session_id,
        ]
        # 默认保留 WorkBuddy 会话，用户可以在会话列表中打开它。
        # 临时模式必须由调用方显式选择，避免任务执行过程变成黑箱。
        if not self.persist_session:
            args.append("--no-session-persistence")
        if self.model:
            args += ["--model", self.model]
        if self.binding:
            args += ["--agent", self.binding["hostAgent"],
                     "--plugin-dir", self.binding["pluginDir"]]
            if self.binding.get("profileMode") == "frozen-expert-instructions":
                args += ["--system-prompt-file", str(self.store / "attempts" / session_id / "expert-prompt.md"),
                         "--disallowedTools", ",".join(self.binding["disallowedTools"]),
                         "--max-turns", str(self.binding["maxTurns"])]
        if self.task_policy is not None:
            args += cli_arguments(self.task_policy, self.store / "attempts" / session_id / "task-permissions.json")
        args += ["--", prompt]
        return args

    def configure_task(self, task, attempt=None):
        if not self.binding:
            raise ValueError("TASK_EXPERT_BINDING_REQUIRED")
        if task["agent_id"] != self.binding["hostAgent"] or Path(task["workspace"]).resolve() != self.workspace:
            raise ValueError("TASK_EXPERT_BINDING_MISMATCH")
        self.task_policy = build_policy(task["agent_id"], task["workspace"], json.loads(task["permissions_json"]),
            task_id=task["task_id"] if attempt else None, attempt_id=attempt["attempt_id"] if attempt else None)

    def run(self, session_id, prompt):
        """同步跑完一次 CLI 调用，返回原始事件列表与执行事实。

        返回 dict：``rawLines`` / ``events`` / ``exitCode`` / ``timedOut`` / ``bytes``
        """
        directory = self.store / "attempts" / session_id
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.binding and self.binding.get("profileMode") == "frozen-expert-instructions":
            data = Path(self.binding["definitionPath"]).read_bytes()
            if hashlib.sha256(data).hexdigest() != self.binding["definitionSha256"]:
                raise WorkBuddyTransportError("WORKBUDDY_EXPERT_DRIFT")
            _, body = parse_frontmatter(data.decode("utf-8"))
            fd = os.open(directory / "expert-prompt.md", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as frozen:
                frozen.write(body)
        argv = self.argv(session_id, prompt)
        runtime_path = directory / "runtime.json"
        runtime = {"sessionId": session_id, "workspace": str(self.workspace),
                   "requestedModel": self.model, "binding": self.binding,
                   "state": "starting", "startedAt": time.time()}

        def save_runtime(**changes):
            runtime.update(changes, updatedAt=time.time())
            temporary = runtime_path.with_suffix(".tmp")
            temporary.write_text(_json(runtime), encoding="utf-8")
            os.chmod(temporary, 0o600)
            os.replace(temporary, runtime_path)

        save_runtime()
        owner_read, owner_write = os.pipe()
        guard = Path(__file__).resolve().parents[1] / "scripts/d4_workbuddy_guard.py"
        guarded_argv = [sys.executable, "-B", str(guard), "--owner-fd", str(owner_read),
                        "--timeout", str(self.timeout), "--"] + argv
        try:
            child = subprocess.Popen(
                guarded_argv, cwd=str(self.workspace), env=self.env,
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, close_fds=True, pass_fds=(owner_read,), start_new_session=True,
            )
        except OSError as exc:
            os.close(owner_read)
            os.close(owner_write)
            save_runtime(state="unknown", reason="launch_failed", finishedAt=time.time())
            raise WorkBuddyTransportError("WORKBUDDY_LAUNCH_FAILED:%s" % type(exc).__name__) from exc
        os.close(owner_read)
        save_runtime(state="running", pid=child.pid, guarded=True)

        raw_lines = []
        total = 0
        # 看门狗线程：宿主静默不输出时 readline 会一直阻塞，循环体内的 deadline 检查
        # 根本不会执行；必须由外部计时器强制终止，否则超时永不收敛（fail open）。
        timed = {"value": False}

        def _on_timeout():
            timed["value"] = True
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

        watchdog = threading.Timer(self.timeout, _on_timeout)
        watchdog.daemon = True
        watchdog.start()
        stderr_count = {"bytes": 0}

        def drain_stderr():
            try:
                for block in iter(lambda: child.stderr.read(8192), b""):
                    stderr_count["bytes"] += len(block)
                    if stderr_count["bytes"] > self.max_output:
                        _on_timeout()
                        return
            except (OSError, ValueError):
                return

        stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
        stderr_reader.start()
        try:
            live_path = directory / "events.live.jsonl"
            fd = os.open(live_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as live:
                for raw in iter(lambda: child.stdout.readline(self.max_line + 1), b""):
                    total += len(raw)
                    if total > self.max_output or len(raw) > self.max_line:
                        raise WorkBuddyTransportError("WORKBUDDY_OUTPUT_TOO_LARGE")
                    live.write(raw)
                    live.flush()
                    raw_lines.append(raw.decode("utf-8", errors="replace"))
                    event = parse_stream_line(raw_lines[-1])
                    if event.get("type") == "system" and event.get("subtype") == "init":
                        save_runtime(observedModel=event.get("model"),
                                     observedAgent=event.get("agent"),
                                     observedWorkspace=event.get("cwd"),
                                     observedSessionId=event.get("session_id"))
                        if self.model and event.get("model") != self.model:
                            raise WorkBuddyTransportError("WORKBUDDY_MODEL_MISMATCH")
                        if (self.binding and event.get("agent") is not None
                                and event["agent"] != self.binding["hostAgent"]):
                            raise WorkBuddyTransportError("WORKBUDDY_AGENT_MISMATCH")
                        expected_agent = getattr(self, "expected_host_agent", None)
                        if (expected_agent and event.get("agent") is not None
                                and event["agent"] != expected_agent):
                            raise WorkBuddyTransportError("CODEBUDDY_AGENT_MISMATCH")
                        if event.get("cwd") is not None and event["cwd"] != str(self.workspace):
                            raise WorkBuddyTransportError("WORKBUDDY_WORKSPACE_MISMATCH")
                    if event.get("type") == "assistant" and self.model:
                        message = event.get("message")
                        observed_model = message.get("model") if isinstance(message, dict) else None
                        if observed_model is not None and observed_model != self.model:
                            raise WorkBuddyTransportError("WORKBUDDY_MODEL_MISMATCH")
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                timed["value"] = True
        finally:
            os.close(owner_write)
            watchdog.cancel()
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                child.wait(timeout=5)
            stderr_reader.join(timeout=1)
            for stream in (child.stdout, child.stderr):
                if stream and not stream.closed:
                    try:
                        stream.close()
                    except OSError:
                        pass
            save_runtime(state="unknown" if timed["value"] else "exited",
                         exitCode=child.returncode, timedOut=timed["value"],
                         stderrBytes=stderr_count["bytes"],
                         finishedAt=time.time())

        events = [parse_stream_line(line) for line in raw_lines]
        return {
            "argv": argv,
            "rawLines": raw_lines,
            "events": events,
            "exitCode": child.returncode,
            "timedOut": timed["value"],
            "bytes": total,
        }


class WorkBuddyAdapter(HostAdapter):
    """D4 的 WorkBuddy 宿主适配器（``sourceId=workbuddy-codebuddy``）。

    与 QClaw 适配器的语义差异：``codebuddy --print`` 是**会话式同步执行**，
    一次 ``submit`` 会跑完整个 CLI 会话。因此本适配器把"宿主已接收"的证据
    （``system/init`` 的 session_id 回显）与"会话已结束"的事实一起记录，
    但**始终**把降级留给上层：只有产物摘要校验通过，交付才可能成立。
    """

    name = "workbuddy-codebuddy"
    source_id = WORKBUDDY_SOURCE_ID

    def __init__(self, transport, store_dir, *, source_id=WORKBUDDY_SOURCE_ID):
        if transport is None or not callable(getattr(transport, "run", None)):
            raise ValueError("WORKBUDDY_TRANSPORT_REQUIRED")
        if source_id != type(self).source_id:
            raise ValueError("WORKBUDDY_SOURCE_ID_FIXED")
        self.transport = transport
        self.source_id = source_id
        self.store = Path(store_dir).absolute()
        self.store.mkdir(mode=0o700, parents=True, exist_ok=True)
        stat = self.store.stat()
        if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
            raise ContractError(
                "permission_denied",
                message="适配器 store 必须是当前用户私有目录（0700）",
                details={"store": str(self.store)},
            )

    # -- 能力自报 ---------------------------------------------------------
    def capabilities(self):
        return AdapterCapabilities(
            submit=True,
            # 有 session_id 回显作为接收证据；无更强的独立凭证。
            host_acceptance=True,
            stable_ids=True,
            # research-gate §4：--resume/-c 断线恢复未验证，不得声称支持。
            restart_recovery=False,
            evidence_level="LOCAL",
            notes=(
                "本机 codebuddy CLI 2.115.0 实测：stream-json 逐行输出，"
                "--session-id 回显可作稳定运行编号，acceptEdits 下可真实写文件。"
                "默认保留 CLI 会话；不保证出现在 WorkBuddy 桌面聊天列表。"
                "限制：宿主持有凭据；CLI 层不提供 workspace 越界保护（详见 research-gate.md）；"
                "result/success 不等于交付成功。"
            ),
        )

    # -- 提交 -------------------------------------------------------------
    def submit(self, task, attempt):
        self._validate_binding(task, attempt)
        attempt_id = attempt["attempt_id"]
        receipt_path = self._attempt_dir(attempt_id) / "receipt.json"

        # 幂等：同一 attempt 只执行一次，重复提交回放既有回执（§4.2）。
        existing = self._read_receipt(receipt_path)
        if existing is not None:
            if existing.get("hostState") not in ("accepted_by_host", "completed"):
                raise ContractError("submit_unknown", message="既有回执结果未知，不重新提交",
                                    details={"attemptId": attempt_id})
            return self._result_from_receipt(existing, replayed=True)

        session_id = self._session_id_for(attempt_id)
        if getattr(self.transport, "binding", None) or getattr(self.transport, "task_scoped", False):
            self.transport.configure_task(task, attempt)
        prompt = self._build_prompt(task, attempt)
        started_at = time.time()

        try:
            run = self.transport.run(session_id, prompt)
        except WorkBuddyTransportError as exc:
            self._write_receipt(receipt_path, {
                "attemptId": attempt_id, "sessionId": session_id, "hostState": "unknown",
                "reason": str(exc), "submittedAt": started_at, "replayed": False,
            })
            raise ContractError(
                "submit_unknown",
                message="宿主调用结果未知（transport 失败），不自动重试",
                details={"attemptId": attempt_id, "cause": str(exc)},
            ) from exc

        receipt = self._judge(run, attempt_id, session_id, started_at)
        self._persist(attempt_id, run, receipt)
        self._write_receipt(receipt_path, receipt)

        if receipt["hostState"] != "accepted_by_host" and receipt["hostState"] != "completed":
            # 没有接收证据 → 不写 accepted_by_host_at，交由服务层记 unknown。
            code = "host_unconfirmed" if receipt["hostState"] == "submitted" else "submit_unknown"
            raise ContractError(
                code,
                message="宿主未给出可采信的接收证据：%s" % receipt.get("reason"),
                details={"attemptId": attempt_id, "reason": receipt.get("reason")},
            )
        return self._result_from_receipt(receipt, replayed=False)

    # -- 状态 / 产物 -------------------------------------------------------
    def get_status(self, attempt_id):
        receipt = self._read_receipt(self._attempt_dir(attempt_id) / "receipt.json")
        if receipt is None:
            raise ContractError(
                "invalid_task",
                message="该执行轮次没有 WorkBuddy 回执",
                details={"attemptId": attempt_id},
            )
        return receipt

    def collect(self, task, attempt, artifact_paths):
        """收集产物摘要；任何路径不可信即 fail closed。

        校验：realpath 必须落在 workspace 内、必须是 regular file、不得是 symlink、
        必须真实存在。返回按 path 排序的 ``[{path, sha256, size}]``。
        """
        self._validate_binding(task, attempt)
        workspace = Path(task["workspace"]).resolve(strict=True)
        receipt = self.get_status(attempt["attempt_id"])
        if receipt.get("hostState") not in ("accepted_by_host", "completed"):
            raise ContractError(
                "invalid_transition",
                message="宿主未接收或会话未正常结束，不能收集产物",
                details={"attemptId": attempt["attempt_id"], "hostState": receipt.get("hostState")},
            )
        artifacts = []
        for rel in artifact_paths:
            if not isinstance(rel, str) or not rel.strip() or os.path.isabs(rel):
                raise ContractError(
                    "path_escape", message="产物必须是 workspace 内相对路径", details={"entry": rel}
                )
            candidate = Path(os.path.realpath(str(workspace / rel)))
            if candidate != workspace and workspace not in candidate.parents:
                raise ContractError(
                    "path_escape",
                    message="产物越出 workspace",
                    details={"workspace": str(workspace), "entry": rel, "resolved": str(candidate)},
                )
            if (workspace / rel).is_symlink() or candidate.is_symlink():
                raise ContractError(
                    "path_escape", message="产物是符号链接，拒绝", details={"entry": rel}
                )
            if not candidate.is_file():
                raise ContractError(
                    "invalid_task",
                    message="产物不存在或不是普通文件",
                    details={"entry": rel, "resolved": str(candidate)},
                )
            data = candidate.read_bytes()
            artifacts.append({
                "path": rel,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            })
        artifacts.sort(key=lambda item: item["path"])
        return artifacts

    # -- 内部 -------------------------------------------------------------
    @classmethod
    def _validate_binding(cls, task, attempt):
        if not isinstance(task, dict) or not isinstance(attempt, dict):
            raise ContractError("invalid_transition", message="任务与执行轮次数据无效")
        if attempt.get("task_id") != task.get("task_id"):
            raise ContractError(
                "invalid_transition",
                message="执行轮次不属于当前任务",
                details={"taskId": task.get("task_id"), "attemptTaskId": attempt.get("task_id")},
            )
        if task.get("source_id") != cls.source_id:
            raise ContractError(
                "permission_denied",
                message="任务来源不是 %s，适配器拒绝提交" % cls.source_id,
                details={"taskSourceId": task.get("source_id")},
            )

    def _attempt_dir(self, attempt_id):
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise ContractError("invalid_task", message="attemptId 必填")
        if "/" in attempt_id or attempt_id in (".", ".."):
            raise ContractError(
                "path_escape", message="attemptId 不能包含路径分隔符", details={"attemptId": attempt_id}
            )
        path = self.store / "attempts" / attempt_id
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        return path

    @staticmethod
    def _session_id_for(attempt_id):
        # 宿主运行编号必须与投递消息里的 hostRunId 一致：service/file_receipts.py
        # 要求回执 events.json 的 hostRunId == submission_json.host_receipt，
        # 而投递消息用的就是 attemptId。加前缀会让两边对不上，因此这里保持恒等映射。
        # 约束：CLI 要求 session-id 以字母或数字开头，attemptId 形如 att-<hex> 满足。
        return attempt_id

    @staticmethod
    def _build_prompt(task, attempt):
        # 复用 D4 投递消息合同：taskId / attemptId / goal / workspace / permissions / acceptance。
        message = build_task_message(task, attempt)
        heading, marker, receipt = message.rpartition("[本轮文件操作]")
        receipt = receipt.replace("使用 write/read，立即依次完成；不联网、不用 exec、不续轮。",
                                  "先按 goal 和任务卡执行，再用 write/read 回传；工具及改动范围以任务权限为准，不续轮。")
        receipt = receipt.replace("唯一写入目录：/workspace/", "本轮回执目录：" + task["workspace"].rstrip("/") + "/")
        receipt = receipt.replace("工具路径加 /workspace/", "工具路径使用上述 workspace 的绝对路径")
        return heading + marker + receipt

    def _judge(self, run, attempt_id, session_id, started_at):
        """把原始事件流判定为宿主状态；**不产生完成判定**。"""
        events = run["events"]
        init_event = None
        started = False
        result_event = None
        tool_use = 0
        tool_denied = 0
        for event in events:
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init":
                init_event = event
            elif kind == "system" and event.get("subtype") == "status":
                started = True
            elif kind == "assistant":
                started = True
                message = event.get("message")
                blocks = message.get("content") if isinstance(message, dict) else []
                for block in blocks if isinstance(blocks, list) else []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_use += 1
            elif kind == "user":
                message = event.get("message")
                blocks = message.get("content") if isinstance(message, dict) else []
                for block in blocks if isinstance(blocks, list) else []:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    body = block.get("content")
                    if isinstance(body, list):
                        body = " ".join(
                            str(item.get("text") if isinstance(item, dict) else item) for item in body
                        )
                    if isinstance(body, str) and body.startswith(DENIED_PREFIX):
                        tool_denied += 1
            elif kind == "result":
                result_event = event

        receipt = {
            "attemptId": attempt_id,
            "sessionId": session_id,
            "submittedAt": started_at,
            "eventCount": len(events),
            "toolUseCount": tool_use,
            "toolDeniedCount": tool_denied,
            "exitCode": run["exitCode"],
            "timedOut": run["timedOut"],
            "replayed": False,
            "sessionPersistent": "--no-session-persistence" not in run.get("argv", []),
            "requestedModel": getattr(self.transport, "model", None),
            "observedModel": init_event.get("model") if init_event else None,
            "observedWorkspace": init_event.get("cwd") if init_event else None,
            "binding": getattr(self.transport, "binding", None),
            "workspace": str(getattr(self.transport, "workspace", "")),
        }

        if run["timedOut"]:
            receipt.update(hostState="unknown", reason="WORKBUDDY_EXECUTION_TIMEOUT")
            return receipt

        if init_event is None and run["exitCode"] != 0:
            receipt.update(hostState="unknown", reason="HOST_EXITED_BEFORE_INIT")
            return receipt

        # 接收证据：system/init 的 session_id 必须与请求值精确一致。
        observed = init_event.get("session_id") if isinstance(init_event, dict) else None
        if init_event is None:
            receipt.update(hostState="submitted",
                           reason="NO_SYSTEM_INIT_EVENT_NO_HOST_ACCEPTANCE_EVIDENCE")
            return receipt
        if observed != session_id:
            receipt.update(hostState="submitted", reason="SESSION_ID_MISMATCH",
                           observedSessionId=observed)
            return receipt

        receipt["acceptedAt"] = started_at
        receipt["hostState"] = "accepted_by_host"
        if started:
            receipt["startedAt"] = started_at

        result_text = result_event.get("result") if isinstance(result_event, dict) else None
        receipt["resultSubtype"] = result_event.get("subtype") if isinstance(result_event, dict) else None
        receipt["resultIsError"] = result_event.get("is_error") if isinstance(result_event, dict) else None
        receipt["resultDigest"] = _sha256_text(result_text) if isinstance(result_text, str) else None

        if result_event is None:
            # 有接收证据但没有终止事件（断线）→ fail closed，不得当成功。
            receipt.update(hostState="unknown", reason="NO_RESULT_EVENT_CONNECTION_DROPPED")
            return receipt
        if run["exitCode"] != 0:
            receipt.update(hostState="unknown", reason="NONZERO_EXIT_CODE")
            return receipt
        if receipt["resultIsError"] is True:
            receipt.update(hostState="unknown", reason="RESULT_IS_ERROR")
            return receipt

        # 会话正常结束。这**只**说明 CLI 跑完了，不说明任务做完了：
        # 实测 subtype=success 可与"一个工具都没动"同时出现（research-gate §3.4）。
        receipt["hostState"] = "completed"
        receipt["reason"] = "CLI_SESSION_COMPLETED_NOT_A_DELIVERY"
        if tool_use == 0:
            receipt["noToolActivity"] = True
        return receipt

    def _persist(self, attempt_id, run, receipt):
        directory = self._attempt_dir(attempt_id)
        raw_path = directory / "events.jsonl"
        raw_path.write_text("".join(run["rawLines"]), encoding="utf-8")
        os.chmod(raw_path, 0o600)
        safe_path = directory / "events.safe.jsonl"
        safe_path.write_text(
            "".join(_json(safe_event(event)) + "\n" for event in run["events"]), encoding="utf-8"
        )
        os.chmod(safe_path, 0o600)
        receipt["eventsPath"] = str(raw_path)
        receipt["safeEventsPath"] = str(safe_path)
        # argv 含完整 prompt，只在本地回执中留摘要，不落原文（§7 不得写公开报告）。
        receipt["argvDigest"] = _sha256_text(_json(run["argv"]))

    def _write_receipt(self, path, receipt):
        path.write_text(_json(receipt), encoding="utf-8")
        os.chmod(path, 0o600)

    @staticmethod
    def _read_receipt(path):
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None

    def _result_from_receipt(self, receipt, replayed):
        host_state = receipt.get("hostState")
        note_parts = [
            "workbuddy hostState=%s" % host_state,
            "exitCode=%s" % receipt.get("exitCode"),
            "events=%s" % receipt.get("eventCount"),
            "tools=%s/denied=%s" % (receipt.get("toolUseCount"), receipt.get("toolDeniedCount")),
        ]
        if receipt.get("noToolActivity"):
            note_parts.append("NO_TOOL_ACTIVITY")
        if replayed:
            note_parts.append("replayed")
        return AdapterResult(
            status="accepted_by_host",
            source_id=self.source_id,
            host_receipt=receipt["sessionId"],
            note="; ".join(note_parts),
            evidence_level="LOCAL",
        )
