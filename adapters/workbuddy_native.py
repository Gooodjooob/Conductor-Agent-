"""WorkBuddy 原生会话 transport —— 离线合同骨架（任务卡 D4-WB-NATIVE-SESSION-001）。

Research Gate 结论（2026-10-02，见输出根 research-gate.md）：本机**未找到**受支持的
"创建原生会话 / 发送消息 / 读取事件" 官方外部接口。CLI（codebuddy 2.115.0）是官方入口，
但其持久化会话落在独立 CLI 存储（``~/.codebuddy/projects/``），与桌面原生会话存储
（``~/.workbuddy/projects/`` + ``~/.workbuddy/app/sessions.json``）分离，实测互不渗透。

本模块只把 Research Gate 的 fail-closed 语义落成可测代码，**不接入 d4ctl、不进入生产派发
路径**（任务卡 §3：能力被阻断时不得进入生产实现，也不得宣称桌面可见）：

- 默认（未注入受支持的原生 transport）一切原生会话操作直接拒绝，
  错误码 ``NATIVE_SESSION_CAPABILITY_BLOCKED``，绝不伪造 conversationId。
- 即使未来注入了 transport，宿主返回的会话编号也必须通过格式与来源校验：
  D4 自报的 attemptId（``att-<hex>``）永远不能冒充 conversationId；
  只有 ``create_conversation`` 真实返回过的编号才被登记为受信会话。
- 发送任务必须带幂等键；同一幂等键不产生第二次执行（§4.4）。
- 任何未知结果一律 ``unknown``，不自动重试（§4.4）。
"""
from __future__ import annotations

import re

# 与 adapters/workbuddy.py 同源的受控错误类型，保持合同一致。
from contracts.schemas import ContractError

# 能力门状态：Research Gate 2026-10-02 结论为 BLOCKED。
NATIVE_SESSION_CAPABILITY = "NATIVE_SESSION_CAPABILITY_BLOCKED"

# 任务卡 §4.2 要求区分的宿主状态（adapter 自报，与 D4 attempt.state 解耦）。
HOST_STATES = (
    "submitted",
    "accepted_by_host",
    "conversation_created",
    "started",
    "progress",
    "delivered",
    "unknown",
)

# 宿主 conversationId 的格式约束：WorkBuddy 桌面端实测为 UUID v4 形态；
# CLI 接受的 --session-id 字符集更宽（字母数字开头，可含 - _ :）。
# 校验目标不是猜格式，而是**排除已知不可信形态**：D4 attemptId 与空值。
_ATTEMPT_ID_RE = re.compile(r"^att-[0-9a-f]+$")

# 必须由 transport 实现并明确支持的接口（任务卡 §4.2）。
REQUIRED_TRANSPORT_METHODS = (
    "create_conversation",
    "send_task",
    "read_events",
    "get_conversation_link",
    "close_or_detach",
)


def is_d4_attempt_id(value):
    """判断一个编号是否是 D4 自报的 attemptId 形态（``att-<hex>``）。"""
    return isinstance(value, str) and bool(_ATTEMPT_ID_RE.match(value))


def validate_host_conversation_id(value):
    """宿主 conversationId 校验：格式 + 来源双重门槛。

    拒绝：非字符串、空值、D4 attemptId 形态。通过则原样返回。
    """
    if not isinstance(value, str) or not value.strip():
        raise ContractError(
            "invalid_transition",
            message="宿主 conversationId 缺失或不是字符串，fail closed",
            details={"observed": repr(value)[:80]},
        )
    if is_d4_attempt_id(value):
        raise ContractError(
            "permission_denied",
            message="CLI --session-id / attemptId 不能冒充 WorkBuddy conversationId",
            details={"observed": value},
        )
    return value


class NativeSessionTransportError(RuntimeError):
    """原生 transport 层错误（不可用 / 协议不符 / 权限不足）。"""


class NativeSessionGate:
    """原生会话能力门：默认阻断，注入 transport 后仍执行宿主编号校验。

    本类不实现任何真正的会话协议；它只负责把任务卡 §4.1 / §4.4 的安全语义
    变成离线可测的合同：
    1. 无 transport → 一切操作 fail closed（NATIVE_SESSION_CAPABILITY_BLOCKED）。
    2. transport 返回的编号必须通过 ``validate_host_conversation_id``。
    3. 只有 gate 登记过的会话才允许 send/read/close（来源校验）。
    4. send 幂等：同一幂等键重复发送回放首次结果，不二次投递。
    """

    capability = NATIVE_SESSION_CAPABILITY

    def __init__(self, transport=None):
        if transport is not None:
            missing = [m for m in REQUIRED_TRANSPORT_METHODS
                       if not callable(getattr(transport, m, None))]
            if missing:
                raise ValueError(
                    "WORKBUDDY_NATIVE_TRANSPORT_INCOMPLETE:%s" % ",".join(missing)
                )
        self._transport = transport
        # 受信会话登记表：conversationId -> 幂等键 -> 首次发送结果。
        self._trusted = {}
        self._idempotency = {}

    # -- 能力自报 ---------------------------------------------------------
    def capability_report(self):
        return {
            "capability": self.capability,
            "transportInjected": self._transport is not None,
            "note": (
                "Research Gate 2026-10-02：未找到受支持的原生会话创建接口；"
                "CLI 会话与桌面原生会话存储分离（LOCAL 证据）。"
                "在官方接口落地前，本门默认阻断一切原生会话操作。"
            ),
        }

    # -- 会话生命周期 -----------------------------------------------------
    def create_conversation(self, workspace, title):
        """创建原生会话。无 transport 时 fail closed。"""
        if self._transport is None:
            raise ContractError(
                "permission_denied",
                message="未找到受支持的 WorkBuddy 原生会话接口，不能创建原生会话",
                details={"capability": self.capability},
            )
        raw = self._transport.create_conversation(workspace, title)
        conversation_id = validate_host_conversation_id(raw)
        self._trusted.setdefault(conversation_id, {})
        return conversation_id

    def send_task(self, conversation_id, task_message, idempotency_key):
        """发送任务。编号必须已由 create 登记；幂等键重复直接回放。"""
        self._require_trusted(conversation_id)
        prior = self._idempotency.get(idempotency_key)
        if prior is not None:
            result = dict(prior)
            result["replayed"] = True
            return result
        result = self._transport.send_task(conversation_id, task_message, idempotency_key)
        if not isinstance(result, dict) or result.get("state") not in HOST_STATES:
            raise ContractError(
                "submit_unknown",
                message="transport 返回无法判读，fail closed 为 unknown，不自动重试",
                details={"observedType": type(result).__name__},
            )
        self._idempotency[idempotency_key] = dict(result)
        return result

    def read_events(self, conversation_id, cursor):
        """读取会话事件。编号未登记或 transport 异常一律 fail closed。"""
        self._require_trusted(conversation_id)
        return self._transport.read_events(conversation_id, cursor)

    def get_conversation_link(self, conversation_id):
        """会话链接（宿主若提供）。未登记编号拒绝。"""
        self._require_trusted(conversation_id)
        return self._transport.get_conversation_link(conversation_id)

    def close_or_detach(self, conversation_id):
        """解除绑定，不强制关闭用户正在查看的会话。"""
        self._require_trusted(conversation_id)
        return self._transport.close_or_detach(conversation_id)

    # -- 内部 -------------------------------------------------------------
    def _require_trusted(self, conversation_id):
        checked = validate_host_conversation_id(conversation_id)
        if checked not in self._trusted:
            raise ContractError(
                "permission_denied",
                message="会话编号未经本门 create_conversation 登记，拒绝操作（来源校验）",
                details={"conversationId": checked},
            )
