"""MessageManager：完整对话落盘（Recall）+ 检索。

wire（core/llm.py::Message）→ schemas Message（补 id/created_at）→ messages 表。
双写向量由 embedder 参数提供（复用 RAG bge）；None 时仅 SQL 检索。
"""
from __future__ import annotations

from datetime import datetime, timezone

from paperflow.core.llm import Message as WireMessage
from paperflow.core.memory.orm import message as message_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.message import Message, MessageRole

__all__ = ["MessageManager"]


def _wire_to_schema(wire: WireMessage) -> Message:
    return Message(
        role=MessageRole(wire.role),
        content=wire.content,
        tool_calls=wire.tool_calls or [],
        tool_call_id=wire.tool_call_id,
        created_at=datetime.now(timezone.utc),
    )


def _row_to_schema(row: dict) -> Message:
    import json
    # content 落盘恒为字符串（_wire_to_schema 只产出 str；schema content 为 str|None），
    # 直接原样回放即可——不能对以 {/[ 开头的字符串内容做 json.loads，否则会把它
    # 变成 dict/list，Message.content 类型校验（str|None）直接抛 ValidationError。
    return Message(
        id=row["id"], role=MessageRole(row["role"]), content=row["content"],
        tool_calls=json.loads(row["tool_calls"]) if row["tool_calls"] else [],
        tool_call_id=row["tool_call_id"], step_id=row["step_id"],
        run_id=row["run_id"], otid=row["otid"], created_at=row["created_at"],
    )


class MessageManager:
    def __init__(self, db: MemoryDB, embedder=None):
        self.db = db
        self.embedder = embedder          # 可选：bge embedder（语义检索）

    def add_message(self, agent_id: str, wire: WireMessage) -> Message:
        m = _wire_to_schema(wire)
        message_orm.insert_message(self.db, agent_id, m)
        return m

    def get_messages_by_agent_id(self, agent_id: str,
                                 limit: int | None = None) -> list[Message]:
        rows = message_orm.select_messages_by_agent(self.db, agent_id, limit=limit)
        return [_row_to_schema(r) for r in rows]

    def get_in_context_messages(self, agent_id: str,
                                limit: int | None = None) -> list[Message]:
        """回放该 agent 的 in-context 消息（当前 = 全部持久化消息；由 compaction
        决定哪些留在窗口——被驱逐的只影响 agent.messages，不删 SQL 行）。"""
        return self.get_messages_by_agent_id(agent_id, limit=limit)

    def search_messages(self, agent_id: str, query: str,
                        roles: list[str] | None = None, limit: int = 5,
                        start_date: str | None = None,
                        end_date: str | None = None) -> list[Message]:
        rows = message_orm.search_messages(self.db, agent_id, query, roles=roles,
                                           limit=limit, start_date=start_date,
                                           end_date=end_date)
        return [_row_to_schema(r) for r in rows]

    def size(self, agent_id: str) -> int:
        return message_orm.count_messages(self.db, agent_id)

    def list_user_messages_for_agent(self, agent_id: str) -> list[Message]:
        rows = message_orm.select_messages_by_agent(self.db, agent_id)
        return [_row_to_schema(r) for r in rows if r["role"] == "user"]
