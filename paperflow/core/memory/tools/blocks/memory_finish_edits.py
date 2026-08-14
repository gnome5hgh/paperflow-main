"""MemoryFinishEditsTool：结束本次记忆编辑的结束信号（无实际写入）。

LLM 用显式调用它声明「本轮记忆编辑完毕」，便于调用方判断编辑会话是否收尾。
"""
from paperflow.core.tool import Tool, ToolResult


class MemoryFinishEditsTool(Tool):
    name = "memory_finish_edits"
    description = "结束本次记忆编辑"
    parameters = {"type": "object", "properties": {}}
    risk_level = "medium"    # 变异/信号类工具一律 medium；只有检索类工具为 low

    def execute(self) -> ToolResult:
        """返回固定完成文案，无副作用。"""
        return ToolResult(text="Memory edits complete.")
