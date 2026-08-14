"""ArchivalMemorySearchTool：检索长期记忆（语义 + tags 过滤）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context


class ArchivalMemorySearchTool(Tool):
    name = "archival_memory_search"
    description = "检索长期记忆（语义 + tags 过滤）"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "top_k": {"type": "integer"},
        },
        "required": ["query"],
    }
    risk_level = "low"    # 纯只读检索

    def execute(self, query: str, tags: list[str] | None = None,
                top_k: int = 10) -> ToolResult:
        """按语义 + tags 检索长期记忆，命中的 passage 以列表形式返回。"""
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            hits = ctx.passage_manager.search_passages(
                ctx.agent_id, query, tags=tags, top_k=top_k)
            if not hits:
                return ToolResult(text="No archival memory found.")
            return ToolResult(text="\n".join(f"- [{p.id}] {p.text}" for p in hits))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
