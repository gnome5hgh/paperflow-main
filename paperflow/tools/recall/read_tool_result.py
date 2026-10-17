"""ReadToolResultTool：按 tool_call_id 或对象键取回被外置的工具原文。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.tool.services.tool_result_store import session_key_prefix
from paperflow.tools.memory.runtime_context import get_memory_context

__all__ = ["ReadToolResultTool"]


class ReadToolResultTool(Tool):
    """取回被外置的大工具结果。

    Attributes:
        name: str，工具名 "read_tool_result"
        description: str，工具描述
        parameters: dict，JSON Schema（tool_call_id / archive_key / offset / limit）
        risk_level: str，"low"（纯只读）
    """

    name = "read_tool_result"
    description = "取回被外置的工具结果原文（按 tool_call_id 或对象键）"
    parameters = {
        "type": "object",
        "properties": {
            "tool_call_id": {"type": "string"},
            "archive_key": {"type": "string"},
            "offset": {"type": "integer"},
            "limit": {"type": "integer"},
        },
    }
    risk_level = "low"

    def execute(self, tool_call_id: str | None = None,
                archive_key: str | None = None,
                offset: int = 0, limit: int | None = None) -> ToolResult:
        """按调用 ID 或对象键取回原文；可分段读取。

        Args:
            tool_call_id: 工具调用 ID（与当前会话组合定位存档）。
            archive_key: 对象键；与 tool_call_id 二选一。
            offset: 起始字符位置。
            limit: 最多读取字符数；None 表示读到末尾。

        Returns:
            ToolResult，文本为原文片段；未找到或未装配时返回明确提示。
        """
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，工具结果不可用")
        store = getattr(ctx, "tool_result_store", None)
        if store is None:
            return ToolResult(text="工具结果存储未装配")
        if archive_key:
            if not archive_key.startswith(session_key_prefix(ctx.agent_id)):
                return ToolResult(text="archive_key 不属于当前会话")
            text = store.get_key(archive_key)
        elif tool_call_id:
            text = store.get(ctx.agent_id, tool_call_id)
        else:
            return ToolResult(text="需要 tool_call_id 或 archive_key")
        if text is None:
            return ToolResult(text="未找到对应的工具结果")
        start = max(0, int(offset or 0))
        end = start + int(limit) if limit else None
        return ToolResult(text=text[start:end])
