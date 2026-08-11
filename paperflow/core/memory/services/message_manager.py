"""MessageManager：完整对话落盘（Recall）+ 检索。

wire（core/llm.py::Message）→ schemas Message（补 id/created_at）→ messages 表。
双写向量由 embedder 参数提供（复用 RAG bge）；None 时仅 SQL 检索。
"""
from __future__ import annotations

import logging
from dataclasses import replace
from datetime import datetime, timezone

from paperflow.core.llm import Message as WireMessage
from paperflow.core.memory.orm import message as message_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.message import Message, MessageRole
from paperflow.core.security.text import sanitize_surrogates

__all__ = ["MessageManager"]

logger = logging.getLogger(__name__)


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
    def __init__(self, db: MemoryDB, embedder=None, agent_manager=None):
        self.db = db
        self.embedder = embedder          # 可选：bge embedder（语义检索）
        self.agent_manager = agent_manager  # 可选：读 AgentState.message_ids（in-context 窗口）

    def add_message(self, agent_id: str, wire: WireMessage) -> Message:
        # 信任边界：清洗代理码点（surrogateescape 残留）——messages 表严格 UTF-8
        # 写入，代理会炸。ask_user 回答等路径不经 agent.run 清洗，add_message 是
        # 全部消息落盘的单点，在此堵漏（replace 生成副本，不改调用方 wire）。
        wire = replace(wire, content=sanitize_surrogates(wire.content))
        m = _wire_to_schema(wire)
        message_orm.insert_message(self.db, agent_id, m)
        return m

    def make_ask_recorder(self, base_ask, agent_id):
        """包装 ask_user 回调：读答案同时把 Q&A 记进 messages 表（role=user）。

        子 agent（writer/qa-agent）无独立 message_manager，其 ask_user 问答本会随
        spawn 结束丢失；统一在此记录 → Sleeptime 可整合进 human 块。记录失败
        fail-safe（不阻断提问），answer 原样透传。
        """
        def ask(question: str) -> str:
            answer = base_ask(question)
            try:
                self.add_message(agent_id, WireMessage(
                    role="user", content=f"[ask_user] {question}\n{answer}"))
            except Exception:
                logger.warning("记录 ask_user 问答失败", exc_info=True)
            return answer
        return ask

    def get_messages_by_agent_id(self, agent_id: str,
                                 limit: int | None = None) -> list[Message]:
        rows = message_orm.select_messages_by_agent(self.db, agent_id, limit=limit)
        return [_row_to_schema(r) for r in rows]

    def get_in_context_messages(self, agent_id: str,
                                limit: int | None = None) -> list[Message]:
        """回放该 agent 的 in-context 消息（AgentState.message_ids 指定的窗口）。

        有 agent_manager 且 message_ids 非空时按 id 返回（压缩后的窗口：摘要 + 保留
        尾部）；message_ids 为空（首轮/未压缩）返回全部持久化消息——兼容。被驱逐的
        旧消息只移出窗口，不删 SQL 行（Recall 完整可追溯）。"""
        if self.agent_manager is not None:
            try:
                state = self.agent_manager.get_agent(agent_id)
                ids = state.message_ids
            except KeyError:
                ids = []
            if ids:
                rows = message_orm.select_messages_by_ids(self.db, ids)
                return [_row_to_schema(r) for r in rows]
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
