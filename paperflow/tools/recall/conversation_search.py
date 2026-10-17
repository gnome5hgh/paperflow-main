"""ConversationSearchTool：检索当前会话的完整对话（Recall）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.memory.runtime_context import get_memory_context

__all__ = ["ConversationSearchTool"]


class ConversationSearchTool(Tool):
    """检索当前会话的对话历史（含 tool 消息，按需开启）。

    Attributes:
        name: str，工具名 "conversation_search"
        description: str，工具描述
        parameters: dict，JSON Schema（query/roles/limit/start_date/end_date）
        risk_level: str，"low"（纯只读检索）
    """

    name = "conversation_search"
    description = (
        "检索当前会话的完整对话历史（Recall）；摘要是有损地图，需要精确措辞、"
        "原文或错误信息时用本工具查当前会话。")
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "roles": {"type": "array", "items": {"type": "string"}},
            "limit": {"type": "integer"},
            "start_date": {"type": "string"},
            "end_date": {"type": "string"},
        },
        "required": ["query"],
    }
    risk_level = "low"    # 纯只读检索

    def execute(self, query: str, roles: list[str] | None = None,
                limit: int = 5, start_date: str | None = None,
                end_date: str | None = None) -> ToolResult:
        """按内容检索当前会话；默认只搜 user/assistant，tool 需显式指定。

        默认过滤 tool 消息是为了避免把大量工具回执塞回上下文；需要核对工具原文时
        显式传 roles=["tool"]。检索永远限定在当前会话，不提供跨会话参数。

        Args:
            query: str，检索词
            roles: list[str] | None，限定角色（缺省 user/assistant）
            limit: int，返回条数上限
            start_date: str | None，起始时间（ISO 字符串）
            end_date: str | None，结束时间（ISO 字符串）

        Returns:
            ToolResult，文本为命中的对话内容；无命中返回固定提示。
        """
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            if roles is None:
                roles = ["user", "assistant"]    # 过滤 tool 消息，防回声/递归放大
            hits = ctx.message_manager.search_messages(
                None, query, roles=roles, limit=limit,
                start_date=start_date, end_date=end_date,
                session_id=ctx.agent_id)
            if not hits:
                return ToolResult(text="No conversation matches found.")
            return ToolResult(text="\n".join(
                f"[{m.role.value}] {m.content}" for m in hits))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
