"""QClaw(OpenClaw) Gateway adapter — D4-VAL-002-BRIDGE 最小桥接切片。

边界（任务卡 D4-VAL-002-BRIDGE §4 G2/G4）：
- 本模块只依赖可注入的 ``GatewayTransport.call(method, params, timeout_ms)``。
- 默认 CLI transport 只用绝对路径、shell=False；凭据（state dir/config path）只走
  环境变量（OPENCLAW_STATE_DIR / OPENCLAW_CONFIG_PATH），绝不进 argv。
- 模拟通道回执必须带 ``meta.transport == "gateway"``。真实 CLI 的
  ``gateway call agent`` 响应不含该字段，来源由受控 CLI transport 证明；
  两种通道都必须核对 ``runId`` 与本次幂等键一致。
- 实际 Agent id 固定为受控常量 D4_GATEWAY_AGENT_ID（agent-d4-exec-002，D4 专用隔离 Agent），
  不从任务 payload 自报读取，也不接受调用方注入（构造函数无覆盖参数）。
- ``agent.wait`` / ``tasks.list`` / ``chat.history`` 仅接口预留，fail closed；
  不把终态快照伪装成 task.started。
- 离线 mock transport 与真实 CLI 的区分不在本模块（注入的 transport 无法自证真伪）：
  数据层由调用入口的 synthetic 标记承担；capability_report 如实声明"真实宿主未验证"。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from adapters.base import AdapterCapabilities, AdapterResult, HostAdapter
from contracts.paths import resolve_scopes
from contracts.schemas import ContractError
from service.file_receipts import receipt_directory

GATEWAY_TRANSPORT_LABEL = "gateway"
ACCEPTED_STATUSES = ("accepted", "in_flight")

# D4-AGENT-ISOLATION-GATE G1：D4 专用宿主 Agent 固定映射。
# - 逻辑身份不变（任务/事件/审核仍是 dev-1 / qclaw-gateway / cox）；
# - 发往 Gateway 的宿主 Agent ID 只来自这个受控常量：不从任务 payload、事件文件
#   或 review payload 自报读取，构造函数也不提供覆盖参数（传入会 TypeError）；
# - 此专用 Agent 已在本机配置并通过 smoke-004 隔离验证；其他安装仍需独立核验，
#   若 ID 被占用或 QClaw 不接受，停止并交 cox 裁定，不得自行换名。
D4_GATEWAY_AGENT_ID = "agent-d4-exec-002"
# 向后兼容别名：旧引用点的语义已并入上面的固定映射。
DEFAULT_GATEWAY_AGENT_ID = D4_GATEWAY_AGENT_ID
DEFAULT_SUBMIT_TIMEOUT_MS = 15000
D4_GATE_CHECKER = Path(__file__).resolve().parents[1] / "scripts" / "d4_isolation_check.js"


class GatewayTransportError(RuntimeError):
    """transport 层错误（超时/启动失败/非零退出/输出不可解析）。

    调用方 fail closed：记 submit_unknown / relay 未发送，不自动重试。
    """


def _dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_gateway_json(stdout):
    """解析 gateway call 的 stdout：过滤 proxy-bootstrap 行后取第一个 JSON 对象。

    与 shared/agent-orchestrator/orchestrator/openclaw.py 的解析口径保持一致。
    """
    lines = [ln for ln in str(stdout or "").splitlines()
             if not ln.lstrip().startswith("[proxy-bootstrap]")]
    starts = [0] + [i for i, ln in enumerate(lines[1:], 1) if ln.lstrip().startswith("{")]
    last_error = None
    for start in starts:
        candidate = "\n".join(lines[start:]).strip()
        if not candidate:
            continue
        try:
            value = json.loads(candidate)
        except ValueError as exc:
            last_error = exc
            continue
        if isinstance(value, dict):
            return value
        last_error = ValueError("response must be a JSON object")
    raise GatewayTransportError(
        "GATEWAY_INVALID_JSON" + (": %s" % (last_error,) if last_error else "")
    )


def validate_gateway_receipt(response, idempotency_key, *, trusted_gateway=False,
                             expected_session_key=None):
    """校验 gateway ``agent`` 调用回执；通过则返回 ``(run_id, status)``。

    采信条件（缺一即 fail closed）：
    - 回执是对象；模拟通道须有 ``meta.transport == "gateway"``；真实 CLI
      transport 可以无 meta，但显式标注非 gateway 仍拒绝
    - ``runId`` 非空且与本次幂等键一致（防串号）
    - ``status`` ∈ accepted / in_flight
    - 真实 CLI 通道还须与预期 sessionKey 一致

    其余情况（包括 completed 等终态快照）一律不记接收：终态不是接收证据。
    """
    if not isinstance(response, dict):
        raise ContractError(
            "host_unconfirmed",
            message="gateway 回执必须是对象，不予采信",
            details={"got": type(response).__name__},
        )
    meta = response.get("meta")
    gateway_label = isinstance(meta, dict) and meta.get("transport") == GATEWAY_TRANSPORT_LABEL
    if not gateway_label and not (trusted_gateway and meta is None):
        raise ContractError(
            "host_unconfirmed",
            message="回执缺少 gateway transport 标记，不予采信",
            details={"meta": meta if isinstance(meta, dict) else None,
                     "reason": "transport_label_missing"},
        )
    run_id = response.get("runId")
    if not isinstance(run_id, str) or not run_id.strip():
        raise ContractError(
            "host_unconfirmed",
            message="回执缺少 runId，不予采信",
            details={"status": response.get("status"), "reason": "run_id_missing"},
        )
    if run_id != idempotency_key:
        raise ContractError(
            "host_unconfirmed",
            message="回执 runId 与本次提交幂等键不一致，不予采信",
            details={"reason": "run_id_mismatch", "expected": idempotency_key, "got": run_id},
        )
    if expected_session_key is not None and response.get("sessionKey") != expected_session_key:
        raise ContractError(
            "host_unconfirmed",
            message="回执 sessionKey 与本次任务不一致，不予采信",
            details={"reason": "session_key_mismatch"},
        )
    status = response.get("status")
    if status not in ACCEPTED_STATUSES:
        raise ContractError(
            "host_unconfirmed",
            message="回执 status %r 不能记为宿主已接收" % (status,),
            details={"reason": "status_not_accepted", "status": status, "runId": run_id},
        )
    return run_id, status


def _permissions_digest(task):
    """权限原文摘要：与库内 permissions_json 一致，不凭空增删。"""
    try:
        permissions = json.loads(task.get("permissions_json") or "{}")
    except ValueError:
        permissions = {}
    return permissions if isinstance(permissions, dict) else {}


def build_task_message(task, attempt):
    """任务投递消息：worker 收到的内容与库内任务/权限一致。"""
    lines = [
        "[D4 任务提交]",
        "taskId: %s" % task["task_id"],
        "attemptId: %s" % attempt["attempt_id"],
        "goal: %s" % task["goal"],
        "workspace: %s" % task["workspace"],
        "permissions: %s" % _dump(_permissions_digest(task)),
    ]
    acceptance = json.loads(task["acceptance_json"] or "[]")
    if isinstance(acceptance, list):
        for index, item in enumerate(acceptance, 1):
            lines.append("acceptance[%d]: %s" % (index, item))
    lines.append("说明: 请基于上述 attemptId 对应的执行轮次执行并交付。")
    lines.append(build_receipt_message(task, attempt, attempt["attempt_id"]))
    return "\n".join(lines)


def build_decision_message(task, review, attempt):
    """返工通知：必须包含原 taskId、新 attemptId、返工说明与权限原文摘要。"""
    return "\n".join([
        "[D4 返工通知]",
        "taskId: %s" % task["task_id"],
        "attemptId: %s" % attempt["attempt_id"],
        "返工说明: %s" % (review.get("note") or ""),
        "workspace: %s" % task["workspace"],
        "permissions: %s" % _dump(_permissions_digest(task)),
        "说明: 请基于新 attemptId 重新执行并交付。",
        build_receipt_message(task, attempt, "relay-%s" % review["review_id"]),
    ])


def build_receipt_message(task, attempt, run_id):
    directory = receipt_directory(task["task_id"], attempt["attempt_id"])
    event = {
        "eventId": "evt-%s-1" % attempt["attempt_id"], "projectId": task["project_id"],
        "taskId": task["task_id"], "attemptId": attempt["attempt_id"],
        "sourceId": task["source_id"], "agentId": task["agent_id"], "sequence": 1,
        "type": "task.started", "occurredAt": attempt["created_at"], "payload": {},
    }
    delivered = {**event, "eventId": "evt-%s-2" % attempt["attempt_id"],
                 "sequence": 2, "type": "task.delivered",
                 "payload": {"summary": "填写实际完成内容",
                             "reportPath": directory + "/delivery-report.md",
                             "changedFiles": [directory + "/result.md", directory + "/delivery-report.md"],
                             "verification": ["填写实际读回核验"], "risks": [], "unfinished": []}}
    return "\n".join([
        "[本轮文件操作] 使用 write/read，立即依次完成；不联网、不用 exec、不续轮。",
        "唯一写入目录：/workspace/%s/，不修改旧轮文件。" % directory,
        "1. 用 write 写 events.json：复制下方 JSON，但 events 暂只保留第一个 started 对象。",
        "2. 用 write 写 result.md（完成 goal）和 delivery-report.md（实际改动、验证及限制）。",
        "3. 用 read 读回这两个文件；成功后用 write 把下方完整 JSON 写入 events.json。",
        "填写 summary/verification；保留所有编号、路径及 started 内容。失败则停止并报告错误，不写 delivered。",
        "模板时间是近似自报时间，报告必须声明不是精确执行时间。JSON 文件路径用相对路径，工具路径加 /workspace/。",
        "最终 JSON：" + _dump({"hostRunId": run_id, "events": [event, delivered]}),
    ])


class CliGatewayTransport:
    """``openclaw gateway call`` 的安全 argv transport。

    - command[0] 与 state_dir 必须是绝对路径（受控入口传入并校验后执行）
    - shell=False；state dir / config path 通过环境变量传递，绝不进 argv
    - evidence_level 如实自报 "LOCAL"：本机 CLI 链路；不宣称官方公共 API
    """

    evidence_level = "LOCAL"

    def __init__(self, command, state_dir, config_path=None, cwd=None, env=None,
                 timeout_grace_ms=3000, node_binary=None):
        if not isinstance(command, (list, tuple)) or not command:
            raise ValueError("GATEWAY_COMMAND_REQUIRED")
        if not isinstance(command[0], str) or not Path(command[0]).is_absolute():
            raise ValueError("GATEWAY_COMMAND_MUST_BE_ABSOLUTE")
        if not isinstance(state_dir, str) or not Path(state_dir).is_absolute():
            raise ValueError("GATEWAY_STATE_DIR_MUST_BE_ABSOLUTE")
        self.command = list(command)
        self.state_dir = state_dir
        self.config_path = config_path
        self.cwd = cwd
        self.env = dict(env or {})
        self.timeout_grace_ms = int(timeout_grace_ms)
        self.node_binary = node_binary or shutil.which("node")
        self._approved_key = None

    def preflight(self, task, idempotency_key=None):
        """读取宿主配置并运行共享 D4 门禁；失败绝不放行 agent RPC。"""
        self._approved_key = None
        try:
            permissions = json.loads(task["permissions_json"])
            if not isinstance(permissions, dict):
                raise ValueError("permissions must be an object")
            scopes = permissions.get("writeScope")
            if not isinstance(scopes, list):
                raise ValueError("writeScope must be an array")
            permissions = dict(permissions, writeScope=resolve_scopes(task["workspace"], scopes))
        except (KeyError, TypeError, ValueError, ContractError) as exc:
            raise GatewayTransportError("D4_GATE_REQUEST_INVALID") from exc
        if not self.node_binary or not Path(self.node_binary).is_absolute():
            raise GatewayTransportError("D4_GATE_CHECKER_UNAVAILABLE")
        config_result = self.call("config.get", {}, 10000)
        try:
            completed = subprocess.run(
                [self.node_binary, str(D4_GATE_CHECKER)],
                input=_dump({"permissions": permissions, "configResult": config_result}),
                cwd=self.cwd,
                env=dict(os.environ, **self.env),
                shell=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GatewayTransportError("D4_GATE_CHECKER_UNAVAILABLE") from exc
        try:
            result = json.loads(completed.stdout)
        except ValueError as exc:
            raise GatewayTransportError("D4_GATE_CHECKER_INVALID_RESPONSE") from exc
        if not isinstance(result, dict):
            raise GatewayTransportError("D4_GATE_CHECKER_INVALID_RESPONSE")
        if completed.returncode != 0 or result.get("safe") is not True:
            reason = result.get("reason") if isinstance(result, dict) else None
            raise GatewayTransportError("D4_ISOLATION_GATE_REJECTED: %s" % (reason or "unknown",))
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key:
                raise GatewayTransportError("D4_GATE_IDEMPOTENCY_KEY_INVALID")
            self._approved_key = idempotency_key

    def call(self, method, params, timeout_ms):
        if method == "agent":
            key = params.get("idempotencyKey") if isinstance(params, dict) else None
            if (not self._approved_key or key != self._approved_key
                    or params.get("agentId") != D4_GATEWAY_AGENT_ID):
                raise GatewayTransportError("D4_ISOLATION_GATE_REQUIRED")
            self._approved_key = None
        env = dict(os.environ)
        env.update(self.env)
        env["OPENCLAW_STATE_DIR"] = self.state_dir
        if self.config_path:
            env["OPENCLAW_CONFIG_PATH"] = self.config_path
        argv = self.command + [
            "gateway", "call", str(method),
            "--params", _dump(params if params is not None else {}),
            "--json",
            "--timeout", str(int(timeout_ms)),
        ]
        try:
            completed = subprocess.run(
                argv,
                cwd=self.cwd,
                env=env,
                shell=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=(int(timeout_ms) + self.timeout_grace_ms) / 1000.0,
            )
        except subprocess.TimeoutExpired as exc:
            raise GatewayTransportError("GATEWAY_TIMEOUT") from exc
        except OSError as exc:
            raise GatewayTransportError(
                "GATEWAY_LAUNCH_FAILED: %s" % type(exc).__name__
            ) from exc
        if completed.returncode != 0:
            tail = (completed.stderr or "").strip().splitlines()[-1:]
            raise GatewayTransportError(
                "GATEWAY_EXIT_%d%s" % (
                    completed.returncode,
                    ": %s" % tail[0][:200] if tail else "",
                )
            )
        return _parse_gateway_json(completed.stdout)


class QClawGatewayAdapter(HostAdapter):
    """通过 GatewayTransport 把任务提交/返工决定发给同一受控 Agent session。

    最小切片只实现：
    - ``submit``        : ``gateway call agent``，幂等键 = attemptId
    - ``send_decision``: ``gateway call agent``，幂等键 = relay-<reviewId>
    - ``wait_for_terminal`` / ``list_host_tasks`` / ``fetch_history``: 预留，fail closed
    """

    name = "qclaw-gateway"

    def __init__(self, transport,
                 submit_timeout_ms=DEFAULT_SUBMIT_TIMEOUT_MS,
                 relay_timeout_ms=DEFAULT_SUBMIT_TIMEOUT_MS,
                 task_message_builder=None, decision_message_builder=None):
        if transport is None or not callable(getattr(transport, "call", None)):
            raise ValueError("GATEWAY_TRANSPORT_REQUIRED")
        self.transport = transport
        # G1：宿主 Agent ID 只来自受控常量映射；构造函数无覆盖入口，
        # 旧调用方传入 agent_id 参数会直接 TypeError（fail loud，不静默忽略）。
        self.agent_id = D4_GATEWAY_AGENT_ID
        self.submit_timeout_ms = int(submit_timeout_ms)
        self.relay_timeout_ms = int(relay_timeout_ms)
        self.task_message_builder = task_message_builder or build_task_message
        self.decision_message_builder = decision_message_builder or build_decision_message
        # 单次回执的证据等级随 transport 自报；无声明视为 UNVERIFIED。
        self.evidence_level = str(getattr(transport, "evidence_level", "UNVERIFIED"))

    def session_key(self, task_id):
        """稳定 sessionKey：与真实 gateway 的 session 路由约定一致。"""
        return "agent:%s:d4-%s" % (self.agent_id, task_id)

    def _evidence(self):
        level = self.evidence_level
        return level if level in ("OFFICIAL", "LOCAL") else "UNVERIFIED"

    def capabilities(self):
        return AdapterCapabilities(
            submit=True,
            host_acceptance=True,
            stable_ids=True,
            restart_recovery=False,
            # 静态声明：本适配器尚未在真实宿主上跑过 agent 调用（本卡只允许 mock）。
            evidence_level="UNVERIFIED",
            notes=(
                "gateway call agent 回执（runId 幂等）；单次回执等级随 transport 自报"
                "（LOCAL=本机 CLI 链路）。离线 mock 与真实链路的区分由入口 synthetic 标记承担。"
                "agent.wait/tasks.list/chat.history 仅接口预留。"
            ),
        )

    def submit(self, task, attempt):
        self._validate_attempt_binding(task, attempt)
        attempt_id = attempt["attempt_id"]
        params = {
            "agentId": self.agent_id,
            "sessionKey": self.session_key(task["task_id"]),
            "message": self.task_message_builder(task, attempt),
            "idempotencyKey": attempt_id,
            "deliver": False,
        }
        preflight = getattr(self.transport, "preflight", None)
        if callable(preflight):
            preflight(task, attempt_id)
        response = self.transport.call("agent", params, self.submit_timeout_ms)
        real_cli = isinstance(self.transport, CliGatewayTransport)
        run_id, status = validate_gateway_receipt(
            response, attempt_id, trusted_gateway=real_cli,
            expected_session_key=params["sessionKey"] if real_cli else None,
        )
        return AdapterResult(
            status="accepted_by_host",
            source_id=task["source_id"],
            host_receipt=run_id,
            note="gateway status=%s" % (status,),
            evidence_level=self._evidence(),
        )

    def send_decision(self, task, review, attempt):
        """把 changes_requested 的返工说明发到同一 agentId + sessionKey。

        幂等键 = relay-<reviewId>。受控服务以 CAS 防重复发送；发送结果未知时
        保留 unknown，不自动或人工重发，已接收结果仅回放本地提交事实。
        """
        if review.get("decision") != "changes_requested":
            raise ContractError(
                "invalid_transition",
                message="只有 changes_requested 决定需要回传",
                details={"decision": review.get("decision")},
            )
        new_attempt_id = review.get("resulting_attempt_id")
        if not new_attempt_id:
            raise ContractError(
                "invalid_transition",
                message="该返工决定尚未生成新执行轮次，不能回传",
                details={"reviewId": review.get("review_id")},
            )
        self._validate_relay_binding(task, review, attempt)
        idempotency_key = "relay-%s" % review["review_id"]
        params = {
            "agentId": self.agent_id,
            "sessionKey": self.session_key(task["task_id"]),
            "message": self.decision_message_builder(task, review, attempt),
            "idempotencyKey": idempotency_key,
            "deliver": False,
        }
        preflight = getattr(self.transport, "preflight", None)
        if callable(preflight):
            preflight(task, idempotency_key)
        response = self.transport.call("agent", params, self.relay_timeout_ms)
        real_cli = isinstance(self.transport, CliGatewayTransport)
        run_id, status = validate_gateway_receipt(
            response, idempotency_key, trusted_gateway=real_cli,
            expected_session_key=params["sessionKey"] if real_cli else None,
        )
        return {
            "runId": run_id,
            "status": status,
            "sessionKey": params["sessionKey"],
            "idempotencyKey": idempotency_key,
            "evidenceLevel": self._evidence(),
        }

    @staticmethod
    def _validate_attempt_binding(task, attempt):
        """适配器自身也拒绝把别的任务轮次送到宿主，不能只依赖 CLI。"""
        if not isinstance(task, dict) or not isinstance(attempt, dict):
            raise ContractError("invalid_transition", message="任务与执行轮次数据无效")
        if attempt.get("task_id") != task.get("task_id"):
            raise ContractError(
                "invalid_transition",
                message="执行轮次不属于当前任务",
                details={"taskId": task.get("task_id"), "attemptTaskId": attempt.get("task_id")},
            )

    @classmethod
    def _validate_relay_binding(cls, task, review, attempt):
        cls._validate_attempt_binding(task, attempt)
        if review.get("task_id") != task.get("task_id"):
            raise ContractError(
                "invalid_transition",
                message="审核决定不属于当前任务",
                details={"taskId": task.get("task_id"), "reviewTaskId": review.get("task_id")},
            )
        if review.get("resulting_attempt_id") != attempt.get("attempt_id"):
            raise ContractError(
                "invalid_transition",
                message="审核决定与回传执行轮次不一致",
                details={"reviewAttemptId": review.get("resulting_attempt_id"),
                         "attemptId": attempt.get("attempt_id")},
            )

    # -- 预留接口：本切片一律 fail closed，不把终态快照伪装成 task.started ----
    def wait_for_terminal(self, task, attempt, timeout_ms):
        raise ContractError(
            "invalid_transition",
            message="agent.wait 预留接口，本切片未实现；终态快照不作为接收证据",
            details={"reserved": True, "method": "agent.wait"},
        )

    def list_host_tasks(self, agent_id=None):
        raise ContractError(
            "invalid_transition",
            message="tasks.list 预留接口，本切片未实现",
            details={"reserved": True, "method": "tasks.list"},
        )

    def fetch_history(self, task, attempt):
        raise ContractError(
            "invalid_transition",
            message="chat.history 预留接口，本切片未实现",
            details={"reserved": True, "method": "chat.history"},
        )
