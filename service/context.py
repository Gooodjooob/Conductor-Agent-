"""受控入口的调用者 / 授权上下文。

对应 R1 返工卡“必须修正 #1”：
- 所有服务入口都必须接收一个由**受控入口**注入的 `CallerContext`；默认（None）拒绝。
- 身份与权限只来自上下文，绝不信任 payload 自报的 reviewerId / role。
- 本模块只表达“可信入口的调用契约”；本地注入不等于生产认证已完成。

角色与能力：
- owner / reviewer：可创建任务、提交审核决定（worker 与未知身份不能 approved）。
- worker：可创建/提交自己任务的执行轮次（actor 必须等于 task.agent_id）。
- source：可上报事件，actor 必须等于事件的 sourceId，且等于 task.source_id。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

from contracts.schemas import ContractError

# 能力常量
TASK_CREATE = "task.create"
TASK_EXECUTE = "task.execute"
EVENT_INGEST = "event.ingest"
REVIEW_SUBMIT = "review.submit"

REVIEW_ROLES = ("owner", "reviewer")
WORKER_ROLE = "worker"
SOURCE_ROLE = "source"


@dataclass(frozen=True)
class CallerContext:
    """受控入口注入的调用者身份与能力。

    synthetic=True 表示本次调用使用本地模拟数据/回执；服务会把该标记写入证据，
    以免模拟结果被当成真实链路证据。
    """

    actor_id: str
    role: str
    capabilities: FrozenSet[str] = field(default_factory=frozenset)
    entry: str = "unspecified"
    synthetic: bool = False

    def has(self, capability):
        return capability in self.capabilities

    def require(self, capability):
        if capability not in self.capabilities:
            raise ContractError(
                "permission_denied",
                message="调用者 %s(%s) 缺少能力 %s" % (self.actor_id, self.role, capability),
                details={
                    "actorId": self.actor_id,
                    "role": self.role,
                    "required": capability,
                    "entry": self.entry,
                },
            )
        return self

    def to_dict(self):
        return {
            "actorId": self.actor_id,
            "role": self.role,
            "entry": self.entry,
            "synthetic": self.synthetic,
            "capabilities": sorted(self.capabilities),
        }


def require_context(context, capability):
    """默认拒绝：没有受控入口注入的身份时，任何写操作都 fail closed。"""
    if context is None:
        raise ContractError(
            "permission_denied",
            message="缺少受控入口注入的调用者身份，默认拒绝",
            details={"required": capability, "hint": "必须由受控入口注入 CallerContext"},
        )
    if not isinstance(context, CallerContext):
        raise ContractError(
            "permission_denied",
            message="调用者上下文类型非法",
            details={"type": type(context).__name__},
        )
    if capability is None:
        return context
    return context.require(capability)


def owner_context(actor_id="cox", entry="local-review-cli", synthetic=False):
    return CallerContext(
        actor_id=actor_id,
        role="owner",
        capabilities=frozenset({TASK_CREATE, REVIEW_SUBMIT}),
        entry=entry,
        synthetic=synthetic,
    )


def reviewer_context(actor_id="cox", entry="local-review-cli", synthetic=False):
    return CallerContext(
        actor_id=actor_id,
        role="reviewer",
        capabilities=frozenset({TASK_CREATE, REVIEW_SUBMIT}),
        entry=entry,
        synthetic=synthetic,
    )


def worker_context(actor_id="dev-1", entry="local-worker-cli", synthetic=False):
    return CallerContext(
        actor_id=actor_id,
        role=WORKER_ROLE,
        capabilities=frozenset({TASK_EXECUTE}),
        entry=entry,
        synthetic=synthetic,
    )


def source_context(source_id="qclaw-local", entry="local-event-bridge", synthetic=False):
    return CallerContext(
        actor_id=source_id,
        role=SOURCE_ROLE,
        capabilities=frozenset({EVENT_INGEST}),
        entry=entry,
        synthetic=synthetic,
    )
