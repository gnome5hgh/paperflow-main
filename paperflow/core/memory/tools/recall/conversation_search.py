"""ConversationSearchTool：检索完整对话历史（Recall，默认过滤 tool 消息防递归）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context


class ConversationSearchTool(Tool):
    name = "conversation_search"
    description = "检索完整对话历史（Recall）"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "roles": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"},
        },
        "required": ["query"],
    }
    risk_level = "low"    # 纯只读检索

    def execute(self, query: str, roles: list[str] | None = None,
                limit: int = 5) -> ToolResult:
        """按内容检索历史对话；默认只搜 user/assistant，把 tool 消息排除在检索外。

        默认过滤 tool 消息是为了防递归：agent 看到自己的工具结果会形成回声/
        递归放大，检索历史时只关心真实对话轮。
        """
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            if roles is None:
                roles = ["user", "assistant"]    # 过滤 tool 消息，防回声/递归放大
            hits = ctx.message_manager.search_messages(
                ctx.agent_id, query, roles=roles, limit=limit)
            if not hits:
                return ToolResult(text="No conversation matches found.")
            return ToolResult(text="\n".join(f"[{m.role.value}] {m.content}" for m in hits))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
