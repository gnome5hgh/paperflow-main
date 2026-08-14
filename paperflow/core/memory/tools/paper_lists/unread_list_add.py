"""UnreadListAddTool：把论文加入未读清单。title 必须来自提取链/用户（禁文件名）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context
from paperflow.core.memory.tools.paper_lists._common import append_line


class UnreadListAddTool(Tool):
    name = "unread_list_add"
    description = "把一篇论文加入未读清单，追加 `- 标题 (来源)` 行"
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "source": {"type": "string"},
        },
        "required": ["title"],
    }
    risk_level = "medium"

    def execute(self, title: str = "", source: str = "") -> ToolResult:
        """追加 `- 标题 (来源)` 行到 unread_list 块；title 为空直接拒绝（禁文件名）。"""
        if not title or not title.strip():
            return ToolResult(text="Error: title is required (extract from paper, not filename)")
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            line = f"- {title} ({source})" if source else f"- {title}"
            return ToolResult(text=append_line(ctx.block_manager, "unread_list", line))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
