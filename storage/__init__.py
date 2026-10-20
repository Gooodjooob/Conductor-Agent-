"""D4 轻量本地存储层：SQLite 事务 + 唯一约束。

只使用标准库 sqlite3，不新增依赖、不自造队列/工作流引擎（对齐本卡 §0.2 裁定）。
"""
from .sqlite_store import SqliteStore  # noqa: F401

__all__ = ["SqliteStore"]
