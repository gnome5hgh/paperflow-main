"""记忆工具包：一工具一文件，按功能分组（blocks/recall/paper_lists）。

get_memory_tools() 惰性构建全部 9 个记忆工具（模块级单例）；工具执行时经
runtime_context.get_memory_context() 取运行时上下文。任意 agent 的 tools.py 可
`TOOLS = [...] + get_memory_tools()`。
"""
from __future__ import annotations

import threading

from paperflow.core.tool import Tool
from paperflow.tools.memory.runtime_context import (
    MemoryToolsContext, set_memory_context, get_memory_context)
from paperflow.tools.memory.blocks.memory_replace import MemoryReplaceTool
from paperflow.tools.memory.blocks.memory_insert import MemoryInsertTool
from paperflow.tools.memory.blocks.memory_rethink import MemoryRethinkTool
from paperflow.tools.memory.blocks.memory_finish_edits import MemoryFinishEditsTool
from paperflow.tools.memory.blocks.memory import MemoryTool
from paperflow.tools.memory.blocks.memory_apply_patch import MemoryApplyPatchTool
from paperflow.tools.memory.recall.conversation_search import ConversationSearchTool
from paperflow.tools.memory.paper_lists.unread_list_add import UnreadListAddTool
from paperflow.tools.memory.paper_lists.unread_list_remove import UnreadListRemoveTool

__all__ = [
    "get_memory_tools", "set_memory_context", "get_memory_context", "MemoryToolsContext",
    "MemoryReplaceTool", "MemoryInsertTool", "MemoryRethinkTool", "MemoryFinishEditsTool",
    "MemoryTool", "MemoryApplyPatchTool", "ConversationSearchTool", "UnreadListAddTool",
    "UnreadListRemoveTool",
]

#: 9 个工具类的装配清单（顺序即 get_memory_tools 返回顺序）
_TOOL_CLASSES = [
    MemoryReplaceTool, MemoryInsertTool, MemoryRethinkTool, MemoryFinishEditsTool,
    MemoryTool, MemoryApplyPatchTool, ConversationSearchTool, UnreadListAddTool,
    UnreadListRemoveTool,
]

_tools: list[Tool] | None = None
_tools_lock = threading.Lock()


def get_memory_tools() -> list[Tool]:
    """惰性构建并返回 9 个记忆工具实例（模块级单例，双重检查加锁）。

    每次调用返回新列表（共享同一批无状态工具实例），防止调用方就地增删工具
    污染进程级单例。
    """
    global _tools
    if _tools is None:
        with _tools_lock:
            if _tools is None:
                _tools = [cls() for cls in _TOOL_CLASSES]
    return list(_tools)
