"""ToolManager：播种 / 管理记忆工具（对应 Letta services/tool_manager.py）。

记忆工具 = 工具函数（function_sets/*）+ _FunctionTool 包装成 paperFlow Tool。
bind() 注入服务上下文（block_manager/passage_manager/message_manager/agent_id）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory import constants
from paperflow.core.memory.functions.function_sets import base, archival, conversation
from paperflow.core.tool import Tool, ToolResult

__all__ = ["ToolManager", "MemoryToolsContext"]


@dataclass
class MemoryToolsContext:
    """记忆工具共享上下文（第一参数注入各工具函数）。"""
    agent_id: str = ""
    block_manager: object = None
    passage_manager: object = None
    message_manager: object = None
    title_extractor: object = None


class _FunctionTool(Tool):
    """把 Letta 记忆工具函数包装为 paperFlow Tool（框架级注入用）。"""

    name = ""
    description = ""
    parameters = {}

    def __init__(self, name: str, description: str, parameters: dict,
                 fn: Callable, ctx: MemoryToolsContext):
        self.name = name
        self.description = description
        self.parameters = parameters
        self._fn = fn
        self._ctx = ctx
        # 只读检索工具（search）属低风险，其余记忆编辑属本地写操作
        self.risk_level = "low" if name in ("archival_memory_search", "conversation_search") else "medium"

    def execute(self, **kwargs) -> ToolResult:
        try:
            text = self._fn(self._ctx, **kwargs)
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
        return ToolResult(text=text)


#: 各工具的 JSON Schema 参数定义（对齐 Letta 签名）
_BASE_PARAMS = {
    "memory_replace": {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "记忆块标签"},
            "old_string": {"type": "string", "description": "要替换的旧子串（必须唯一）"},
            "new_string": {"type": "string", "description": "新子串"},
        },
        "required": ["label", "old_string", "new_string"],
    },
    "memory_insert": {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "new_string": {"type": "string"},
            "insert_line": {"type": "integer", "description": "插入行号；-1=末尾，0=开头"},
        },
        "required": ["label", "new_string"],
    },
    "memory_rethink": {
        "type": "object",
        "properties": {"label": {"type": "string"}, "new_memory": {"type": "string"}},
        "required": ["label", "new_memory"],
    },
    "memory_finish_edits": {"type": "object", "properties": {}},
    "memory": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "replace", "delete", "rename"]},
            "label": {"type": "string"},
            "value": {"type": "string"},
        },
        "required": ["action", "label"],
    },
    "memory_apply_patch": {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "patch": {"type": "string", "description": "简化 unified diff"},
        },
        "required": ["label", "patch"],
    },
    "archival_memory_insert": {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["content"],
    },
    "archival_memory_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "top_k": {"type": "integer"},
        },
        "required": ["query"],
    },
    "conversation_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "roles": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    },
}

_FN_MAP = {
    "memory_replace": base.memory_replace,
    "memory_insert": base.memory_insert,
    "memory_rethink": base.memory_rethink,
    "memory_finish_edits": base.memory_finish_edits,
    "memory": base.memory,
    "memory_apply_patch": base.memory_apply_patch,
    "archival_memory_insert": archival.archival_memory_insert,
    "archival_memory_search": archival.archival_memory_search,
    "conversation_search": conversation.conversation_search,
}

_DESCRIPTIONS = {
    "memory_replace": "替换记忆块中的精确子串（old_string 必须唯一）",
    "memory_insert": "在记忆块指定行插入内容",
    "memory_rethink": "整块重写记忆",
    "memory_finish_edits": "结束本次记忆编辑",
    "memory": "统一记忆块管理（create/replace/delete/rename）",
    "memory_apply_patch": "用简化 unified diff 更新记忆块",
    "archival_memory_insert": "写入长期记忆（archival passage，可带 tags）",
    "archival_memory_search": "检索长期记忆（语义 + tags 过滤）",
    "conversation_search": "检索完整对话历史（Recall）",
    "unread_list_add": "把一篇论文加入未读清单，追加 `- 标题 (来源)` 行",
    "unread_list_remove": "把一篇论文移出未读清单，按权威标题删除对应行",
    "history_append": "把一次论文消费事件（精读/写笔记）追加进浏览历史，只追加不改旧",
    "extract_title": "提取论文权威标题（PDF 提取链或用户直接提供，禁文件名）",
}


# list_blocks 工具函数（ctx 参数风格与既有记忆工具一致）
def _unread_list_add(ctx, title, source=""):
    from paperflow.core.memory.functions.function_sets.list_blocks import UnreadListAddTool
    return UnreadListAddTool(ctx).execute(title=title, source=source).text


def _unread_list_remove(ctx, title):
    from paperflow.core.memory.functions.function_sets.list_blocks import UnreadListRemoveTool
    return UnreadListRemoveTool(ctx).execute(title=title).text


def _history_append(ctx, action, title):
    from paperflow.core.memory.functions.function_sets.list_blocks import HistoryAppendTool
    return HistoryAppendTool(ctx).execute(action=action, title=title).text


def _extract_title(ctx, pdf_path=None, title=None):
    if title:            # 用户已给标题 → 直接用（禁文件名的守门在调用方）
        return f"title: {title}\nsource: search"
    ex = ctx.title_extractor
    if ex is None:
        return "Error: title extractor not available"
    r = ex.extract(pdf_path=pdf_path)
    return (f"title: {r.title}\nsource: {r.source}" if r.title
            else "Error: 标题提取失败，请提供论文标题")


class ToolManager:
    def __init__(self, db: MemoryDB):
        self.db = db
        self._ctx = MemoryToolsContext()
        self._tools: dict[str, Tool] = {}

    def bind(self, block_manager, passage_manager, message_manager,
             agent_id: str) -> None:
        """注入服务上下文（CLI 组装点调用）。"""
        self._ctx.block_manager = block_manager
        self._ctx.passage_manager = passage_manager
        self._ctx.message_manager = message_manager
        self._ctx.agent_id = agent_id

    def upsert_base_tools(self) -> None:
        """播种标准记忆工具（BASE_MEMORY_TOOLS + archival + conversation）。"""
        for name in constants.BASE_MEMORY_TOOLS | {
                "archival_memory_insert", "archival_memory_search", "conversation_search"}:
            if name not in _FN_MAP:
                continue   # 清单工具（unread_list_*/history_append）经下方 list_tools 独立注册
            fn = _FN_MAP[name]
            self._tools[name] = _FunctionTool(
                name=name, description=_DESCRIPTIONS[name],
                parameters=_BASE_PARAMS[name], fn=fn, ctx=self._ctx)
        list_tools = [
            ("unread_list_add", {"title": {"type": "string"}, "source": {"type": "string"}},
             ["title"], _unread_list_add),
            ("unread_list_remove", {"title": {"type": "string"}}, ["title"], _unread_list_remove),
            ("history_append", {"action": {"type": "string"}, "title": {"type": "string"}},
             ["action", "title"], _history_append),
            ("extract_title", {"pdf_path": {"type": "string"}, "title": {"type": "string"}},
             [], _extract_title),
        ]
        for name, props, required, fn in list_tools:
            self._tools[name] = _FunctionTool(
                name=name, description=_DESCRIPTIONS[name],
                parameters={"type": "object", "properties": props,
                            "required": required},
                fn=fn, ctx=self._ctx)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def create_tool(self, name: str, description: str, parameters: dict) -> Tool:
        t = _FunctionTool(name=name, description=description, parameters=parameters,
                          fn=_FN_MAP.get(name, lambda ctx, **kw: "unknown tool"),
                          ctx=self._ctx)
        self._tools[name] = t
        return t

    def get_tool(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def execute_tool(self, tool_name: str, args: dict, tool_call_id: str) -> ToolResult:
        """执行记忆工具（协调交给 ToolExecutionManager；测试/独立调用路径）。

        brief 的测试直接走 ToolManager.execute_tool，故在此转发；延迟 import
        避免与 tool_execution_manager 的模块级依赖形成循环。
        """
        from paperflow.core.memory.services.tool_execution_manager import ToolExecutionManager
        return ToolExecutionManager(self).execute_tool(tool_name, args, tool_call_id)
