"""MemoryFinishEditsTool：结束本次记忆编辑（Letta 结束信号，无实际写入）。"""
from paperflow.core.tool import Tool, ToolResult


class MemoryFinishEditsTool(Tool):
    name = "memory_finish_edits"
    description = "结束本次记忆编辑"
    parameters = {"type": "object", "properties": {}}
    risk_level = "medium"    # 对齐现状 _FunctionTool：仅检索工具为 low，其余 medium

    def execute(self) -> ToolResult:
        return ToolResult(text="Memory edits complete.")
