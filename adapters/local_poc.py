"""两种适配器实现。

- LocalPocAdapter      ：本卡允许的本地 PoC，只记录“已提交”，不产生宿主接收证据。
- UnverifiedHostAdapter：真实宿主（如 qclaw-local）能力未验证时的 fail-closed 边界。

本卡 §0.2 / §4 明确：qclaw-local 的提交入口、接收确认、稳定编号、重启恢复和权限
均为 UNVERIFIED，不得模拟成已接通。
"""
from __future__ import annotations

from contracts.schemas import ContractError

from .base import AdapterCapabilities, AdapterResult, HostAdapter


class LocalPocAdapter(HostAdapter):
    """本地 PoC 适配器：不连接任何真实宿主。"""

    def __init__(self, source_id="local-poc"):
        self.source_id = source_id
        self.name = "local-poc"

    def capabilities(self):
        return AdapterCapabilities(
            submit=True,
            host_acceptance=False,
            stable_ids=False,
            restart_recovery=False,
            evidence_level="LOCAL",
            notes="本地 PoC：只记录提交请求；未连接任何真实宿主，无“环境已收到”证据",
        )

    def submit(self, task, attempt):
        return AdapterResult(
            status="submitted",
            source_id=self.source_id,
            note="已提交（本地记录）。无宿主接收确认，状态停在“已提交，等待环境确认”",
            evidence_level="LOCAL",
        )


class UnverifiedHostAdapter(HostAdapter):
    """真实宿主适配器占位：能力未验证时 fail closed，拒绝提交。"""

    def __init__(self, host_name, reason=None):
        self.host_name = host_name
        self.reason = reason or "宿主提交入口/接收确认/稳定编号/重启恢复/权限均未验证"
        self.name = "unverified-host:%s" % (host_name,)

    def capabilities(self):
        return AdapterCapabilities(
            submit=False,
            host_acceptance=False,
            stable_ids=False,
            restart_recovery=False,
            evidence_level="UNVERIFIED",
            notes="%s：%s" % (self.host_name, self.reason),
        )

    def submit(self, task, attempt):
        raise ContractError(
            "host_unconfirmed",
            message="%s 能力未验证，拒绝提交（fail closed）" % (self.host_name,),
            details={"host": self.host_name, "evidence": "UNVERIFIED", "reason": self.reason},
        )
