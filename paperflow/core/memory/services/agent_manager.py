"""AgentManager：agent 生命周期 + refresh_memory（块变更后重编译 system prompt）。

对应 Letta services/agent_manager.py。AgentState 持久化在 SQLite（agent_state 表
为 blocks 之上的一行 JSON 快照——message_ids 等）。paperFlow 单用户，agent 表
简化为按 agent_id 键控。
"""
from __future__ import annotations

import json

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.agent import AgentState
from paperflow.core.memory.schemas.memory import Memory
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.services.message_manager import MessageManager

__all__ = ["AgentManager"]


class AgentManager:
    def __init__(self, db: MemoryDB, block_manager: BlockManager,
                 message_manager: MessageManager):
        self.db = db
        self.block_manager = block_manager
        self.message_manager = message_manager
        db.execute("CREATE TABLE IF NOT EXISTS agent_state ("
                   "agent_id TEXT PRIMARY KEY, name TEXT, description TEXT,"
                   "system TEXT, model TEXT, context_window_limit INTEGER,"
                   "message_ids TEXT, created_at TEXT)")

    def _row_to_state(self, row: dict) -> AgentState:
        return AgentState(
            agent_id=row["agent_id"], name=row["name"], description=row["description"],
            system=row["system"], model=row["model"],
            context_window_limit=row["context_window_limit"],
            message_ids=json.loads(row["message_ids"]) if row["message_ids"] else [],
            memory=Memory(blocks=self.block_manager.list_blocks()),
        )

    def create_agent(self, agent_id: str, name: str | None = None) -> AgentState:
        self.db.execute(
            "INSERT OR REPLACE INTO agent_state (agent_id, name, message_ids, created_at)"
            " VALUES (?,?,?, datetime('now'))",
            (agent_id, name, json.dumps([])))
        return self.get_agent(agent_id)

    def get_agent(self, agent_id: str) -> AgentState:
        cur = self.db.execute("SELECT * FROM agent_state WHERE agent_id=?", (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"agent {agent_id} not found")
        return self._row_to_state(dict(row))

    def update_agent(self, agent_id: str, **kwargs) -> AgentState:
        allowed = {"name", "description", "system", "model", "context_window_limit",
                   "message_ids"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        for key, val in updates.items():
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
            self.db.execute(f"UPDATE agent_state SET {key}=? WHERE agent_id=?",
                            (val, agent_id))
        return self.get_agent(agent_id)

    def refresh_memory(self, agent_id: str) -> None:
        """供 Task 11 agent 集成作为 system 重编译挂点。

        当前 memory 由 get_agent 动态构建（_row_to_state 从 block_manager.list_blocks
        读最新块），因此这里的重赋值只作用于局部变量；真实的重编译逻辑在 Task 11
        agent 集成中接入。
        """
        st = self.get_agent(agent_id)
        st.memory = Memory(blocks=self.block_manager.list_blocks())
