"""适配器抽象：能力报告 + 提交结果。

证据等级沿用团队实践中的标记：OFFICIAL / LOCAL / PATTERN / INFERRED / UNVERIFIED。
除 OFFICIAL / LOCAL 外，不得作为“已接通”依据。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass
class AdapterCapabilities:
    """适配器如实自报的能力。False 表示不具备或未验证。"""

    submit: bool
    host_acceptance: bool
    stable_ids: bool
    restart_recovery: bool
    evidence_level: str
    notes: str = ""

    def to_dict(self):
        return asdict(self)


@dataclass
class AdapterResult:
    """一次提交请求的真实结果。

    status 只能是：
      - "submitted"         ：已发出提交请求
      - "accepted_by_host"  ：宿主明确确认收到
    只有 "submitted" 时，页面必须显示“已提交，等待环境确认”，不能显示“执行中”。
    """

    status: str
    source_id: str
    host_receipt: str = None
    note: str = ""
    evidence_level: str = "UNVERIFIED"

    def to_dict(self):
        return asdict(self)


class HostAdapter:
    """宿主适配器接口。子类必须实现 capabilities() 与 submit()。"""

    name = "abstract"

    def capabilities(self):
        raise NotImplementedError

    def submit(self, task, attempt):
        raise NotImplementedError

    def capability_report(self):
        caps = self.capabilities()
        return {
            "adapter": self.name,
            "capabilities": caps.to_dict(),
            # 只有具备宿主接收确认能力时，才允许被称为“已接通”。
            "connected": bool(caps.host_acceptance and caps.evidence_level in ("OFFICIAL", "LOCAL")),
        }
