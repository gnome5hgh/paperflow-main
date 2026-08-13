"""MemoryRethinkTool：整块重写记忆（与 memory 的 replace 动作共用 rewrite_block）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context
from paperflow.core.memory.tools.blocks._common import rewrite_block


class MemoryRethinkTool(Tool):
    name = "memory_rethink"
    description = "整块重写记忆"
    parameters = {
        "type": "object",
        "properties": {"label": {"type": "string"}, "new_memory": {"type": "string"}},
        "required": ["label", "new_memory"],
    }
    risk_level = "medium"

    def execute(self, label: str, new_memory: str) -> ToolResult:
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            return ToolResult(text=rewrite_block(ctx.block_manager, label, new_memory))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
