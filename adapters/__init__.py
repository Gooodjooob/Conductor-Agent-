"""宿主适配器边界（对齐接口文档 §7）。

第一版适配器只需要证明一条链路：

    submit(task, attempt) -> submitted / accepted_by_host
    receive()             -> task.accepted / started / progress / blocked / delivered
    sendDecision()        -> cox 决定可被 Agent 读取

本切片只实现 submit 与能力报告；receive/sendDecision 通过事件接口由宿主调用。
适配器必须如实报告能力；能力未验证时 fail closed，绝不把“宿主名出现在配置里”当成已接通。
"""
from .base import AdapterCapabilities, AdapterResult, HostAdapter  # noqa: F401
from .local_poc import LocalPocAdapter, UnverifiedHostAdapter  # noqa: F401

__all__ = [
    "AdapterCapabilities",
    "AdapterResult",
    "HostAdapter",
    "LocalPocAdapter",
    "UnverifiedHostAdapter",
]
