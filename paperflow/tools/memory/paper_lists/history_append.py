"""HistoryAppendTool：把论文消费事件追加进浏览历史（只追加不改旧）。

条目格式 `[{时间}] {action}《{title}》`——同论文可多次追加，靠时间/动作区分。
"""
from datetime import datetime

from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.memory.runtime_context import get_memory_context
from paperflow.tools.memory.paper_lists._common import append_line


class HistoryAppendTool(Tool):
    """把论文消费事件追加进浏览历史的工具（只追加不改旧）。

    Attributes:
        name: str，工具名 "history_append"
        description: str，工具描述
        parameters: dict，JSON Schema（action/title）
        risk_level: str，"medium"
    """
    name = "history_append"
    description = "把一次论文消费事件（精读/写笔记）追加进浏览历史，只追加不改旧"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": ["action", "title"],
    }
    risk_level = "medium"

    def execute(self, action: str, title: str) -> ToolResult:
        """以 `[{时间}] {action}《{title}》` 格式追加到 history_list 块。

        Args:
            action: str，消费动作（如精读/写笔记）
            title: str，论文标题

        Returns:
            ToolResult，文本为追加结果或错误说明。
        """
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M")
            line = f"[{now}] {action}《{title}》"
            return ToolResult(text=append_line(ctx.block_manager, "history_list", line))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
