"""ArchivalMemoryInsertTool：写入长期记忆（archival passage，可带 tags）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context


class ArchivalMemoryInsertTool(Tool):
    name = "archival_memory_insert"
    description = "写入长期记忆（archival passage，可带 tags）"
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["content"],
    }
    risk_level = "medium"

    def execute(self, content: str, tags: list[str] | None = None) -> ToolResult:
        """把内容写入 archival 长期记忆；成功返回 passage id。"""
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            p = ctx.passage_manager.insert_passage(ctx.agent_id, content, tags=tags or [])
            return ToolResult(text=f"Stored to archival memory: {p.id}")
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
