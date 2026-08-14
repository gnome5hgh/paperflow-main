"""UnreadListRemoveTool：把论文移出未读清单（按权威标题精确删行）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context
from paperflow.core.memory.tools.paper_lists._common import remove_line_by_key


class UnreadListRemoveTool(Tool):
    name = "unread_list_remove"
    description = "把一篇论文移出未读清单，按权威标题删除对应行"
    parameters = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    risk_level = "medium"

    def execute(self, title: str) -> ToolResult:
        """按权威标题删行；块缺失或行不命中时返回显式错误（不物化空块）。"""
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            return ToolResult(text=remove_line_by_key(ctx.block_manager, "unread_list", title))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
