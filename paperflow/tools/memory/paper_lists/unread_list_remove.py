"""UnreadListRemoveTool：把论文移出未读清单（按权威标题精确删行）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.memory.runtime_context import get_memory_context
from paperflow.tools.memory.paper_lists._common import remove_line_by_key


class UnreadListRemoveTool(Tool):
    """把论文移出未读清单的工具（按权威标题精确删行）。

    Attributes:
        name: str，工具名 "unread_list_remove"
        description: str，工具描述
        parameters: dict，JSON Schema（title）
        risk_level: str，"medium"
    """
    name = "unread_list_remove"
    description = "把一篇论文移出未读清单，按权威标题删除对应行"
    parameters = {
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    }
    risk_level = "medium"

    def execute(self, title: str) -> ToolResult:
        """按权威标题删行；块缺失或行不命中时返回显式错误（不物化空块）。

        Args:
            title: str，论文标题（用于前缀匹配删行）

        Returns:
            ToolResult，文本为删除结果或错误说明（不物化空块）。
        """
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            return ToolResult(text=remove_line_by_key(ctx.block_manager, "unread_list", title))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
