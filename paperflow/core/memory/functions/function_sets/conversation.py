"""对话历史检索工具（命名对齐 Letta function_sets/conversation.py）。

conversation_search 默认过滤 tool 消息防止递归（Letta 同款）。
"""
from __future__ import annotations

__all__ = ["conversation_search"]


def conversation_search(ctx, query: str, roles: list[str] | None = None,
                        limit: int = 5, start_date: str | None = None,
                        end_date: str | None = None) -> str:
    if roles is None:
        roles = ["user", "assistant"]          # 过滤 tool 消息
    hits = ctx.message_manager.search_messages(
        ctx.agent_id, query, roles=roles, limit=limit,
        start_date=start_date, end_date=end_date)
    if not hits:
        return "No conversation matches found."
    return "\n".join(f"[{m.role.value}] {m.content}" for m in hits)
