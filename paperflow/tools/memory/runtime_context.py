"""记忆工具运行时上下文 + 模块级访问器。

工具不持有上下文；execute 时经 get_memory_context() 取运行时绑定的 managers
与归属会话。CLI 启动装配完服务层后 set_memory_context 绑定一次；未绑定返回
None，工具据此降级（统一回「记忆服务未装配」而非抛异常）——工具定义与服务
装配彻底解耦，测试也能传 None 隔离。
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = ["MemoryToolsContext", "set_memory_context", "get_memory_context"]


@dataclass
class MemoryToolsContext:
    """记忆工具共享上下文（各工具 execute 时经 get_memory_context() 取得）。"""

    agent_id: str = ""
    block_manager: object = None
    passage_manager: object = None
    message_manager: object = None
    title_extractor: object = None


_memory_context: MemoryToolsContext | None = None


def set_memory_context(ctx: MemoryToolsContext | None) -> None:
    """绑定/清空运行时上下文（CLI 装配后调用；测试传 None 隔离）。"""
    global _memory_context
    _memory_context = ctx


def get_memory_context() -> MemoryToolsContext | None:
    """返回当前绑定的上下文；未绑定返回 None（工具据此降级）。"""
    return _memory_context
