"""记忆工具包：一工具一文件，按功能分组（blocks/archival/recall/paper_lists）。

get_memory_tools() 惰性构建全部 13 个记忆工具（模块级单例，照 rag 的
get_rag_service）；工具执行时经 runtime_context.get_memory_context() 取运行时
上下文。任意 agent 的 tools.py 可 `TOOLS = [...] + get_memory_tools()`。
"""
from __future__ import annotations

import threading

from paperflow.core.tool import Tool
from paperflow.core.memory.tools.runtime_context import (
    MemoryToolsContext, set_memory_context, get_memory_context)
from paperflow.core.memory.tools.blocks.memory_replace import MemoryReplaceTool
from paperflow.core.memory.tools.blocks.memory_insert import MemoryInsertTool
from paperflow.core.memory.tools.blocks.memory_rethink import MemoryRethinkTool
from paperflow.core.memory.tools.blocks.memory_finish_edits import MemoryFinishEditsTool
from paperflow.core.memory.tools.blocks.memory import MemoryTool
from paperflow.core.memory.tools.blocks.memory_apply_patch import MemoryApplyPatchTool
from paperflow.core.memory.tools.archival.archival_memory_insert import ArchivalMemoryInsertTool
from paperflow.core.memory.tools.archival.archival_memory_search import ArchivalMemorySearchTool
from paperflow.core.memory.tools.recall.conversation_search import ConversationSearchTool
from paperflow.core.memory.tools.paper_lists.unread_list_add import UnreadListAddTool
from paperflow.core.memory.tools.paper_lists.unread_list_remove import UnreadListRemoveTool
from paperflow.core.memory.tools.paper_lists.history_append import HistoryAppendTool
from paperflow.core.memory.tools.paper_lists.extract_title import ExtractTitleTool

__all__ = [
    "get_memory_tools", "set_memory_context", "get_memory_context", "MemoryToolsContext",
    "MemoryReplaceTool", "MemoryInsertTool", "MemoryRethinkTool", "MemoryFinishEditsTool",
    "MemoryTool", "MemoryApplyPatchTool", "ArchivalMemoryInsertTool",
    "ArchivalMemorySearchTool", "ConversationSearchTool", "UnreadListAddTool",
    "UnreadListRemoveTool", "HistoryAppendTool", "ExtractTitleTool",
]

_TOOL_CLASSES = [
    MemoryReplaceTool, MemoryInsertTool, MemoryRethinkTool, MemoryFinishEditsTool,
    MemoryTool, MemoryApplyPatchTool, ArchivalMemoryInsertTool, ArchivalMemorySearchTool,
    ConversationSearchTool, UnreadListAddTool, UnreadListRemoveTool, HistoryAppendTool,
    ExtractTitleTool,
]

_tools: list[Tool] | None = None
_tools_lock = threading.Lock()


def get_memory_tools() -> list[Tool]:
    """惰性构建并返回 13 个记忆工具实例（模块级单例，双重检查加锁）。

    每次调用返回新列表（共享同一批无状态工具实例），防止调用方就地增删工具
    污染进程级单例。
    """
    global _tools
    if _tools is None:
        with _tools_lock:
            if _tools is None:
                _tools = [cls() for cls in _TOOL_CLASSES]
    return list(_tools)
