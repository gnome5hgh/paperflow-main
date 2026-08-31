"""AgentManager：agent 生命周期 + refresh_memory（块变更后重编译 system prompt）。

AgentState 持久化在 SQLite 的 agent_state 表——它是一行以 agent_id 为主键的
JSON 快照，其中 message_ids 记录「当前 in-context 窗口的消息 id 列表」。
单用户场景下 agent 表简化为按 agent_id 键控。
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
    """agent 生命周期业务层：建/查/改 AgentState，并把块最新状态织入 memory。

    职责：
    1. 管理 agent_state 表（CRUD）。
    2. 从块表动态构建 AgentState.memory（每轮读取最新块）。
    3. 提供 refresh_memory 钩子供集成层在块变更后重编译 system prompt。

    注意：AgentState.memory 不持久化到 agent_state 表，而是每次 get_agent 时
    通过 block_manager.list_blocks() 动态生成——保证 memory 始终与块表一致。
    """

    def __init__(self, db: MemoryDB, block_manager: BlockManager,
                 message_manager: MessageManager):
        """初始化并创建 agent_state 表（若不存在）。

        Args:
            db: 数据库连接。
            block_manager: 块管理器（用于动态读取块列表）。
            message_manager: 消息管理器（供后续扩展，当前未使用）。
        """
        self.db = db
        self.block_manager = block_manager
        self.message_manager = message_manager
        # 幂等建表：agent_id 为主键，message_ids 存储 JSON 数组
        db.execute("CREATE TABLE IF NOT EXISTS agent_state ("
                   "agent_id TEXT PRIMARY KEY, name TEXT, description TEXT,"
                   "system TEXT, model TEXT, context_window_limit INTEGER,"
                   "message_ids TEXT, created_at TEXT)")

    def _row_to_state(self, row: dict) -> AgentState:
        """把 DB 行转回 AgentState：memory 从 block_manager 现读最新块动态构建。

        Args:
            row: 数据库行（dict 形式，含 agent_id, name, description, system,
                model, context_window_limit, message_ids, created_at）。

        Returns:
            填充好的 AgentState 实例。

        关键设计：
            - memory 字段不来自 DB，而是通过 block_manager.list_blocks() 实时获取，
              保证了块表变更后立刻反映到 AgentState 中，无需额外刷新。
            - message_ids 从 JSON 字符串还原为列表。
        """
        return AgentState(
            agent_id=row["agent_id"],
            name=row["name"],
            description=row["description"],
            system=row["system"],
            model=row["model"],
            context_window_limit=row["context_window_limit"],
            message_ids=json.loads(row["message_ids"]) if row["message_ids"] else [],
            memory=Memory(blocks=self.block_manager.list_blocks()),
        )

    def create_agent(self, agent_id: str, name: str | None = None) -> AgentState:
        """创建 agent（INSERT OR REPLACE 幂等），初始 message_ids 为空列表。

        Args:
            agent_id: 唯一标识符。
            name: 可选的 agent 名称（可后续更新）。

        Returns:
            新建的 AgentState 实例（通过 get_agent 读取）。

        幂等性：若 agent_id 已存在，则覆盖（相当于重置）。
        """
        self.db.execute(
            "INSERT OR REPLACE INTO agent_state (agent_id, name, message_ids, created_at)"
            " VALUES (?,?,?, datetime('now'))",
            (agent_id, name, json.dumps([])))
        return self.get_agent(agent_id)

    def get_agent(self, agent_id: str) -> AgentState:
        """按 id 取 AgentState；不存在抛 KeyError。

        Args:
            agent_id: 要查询的 agent 标识。

        Returns:
            当前最新的 AgentState（memory 动态构建）。

        Raises:
            KeyError: 当 agent_id 不存在时。
        """
        cur = self.db.execute("SELECT * FROM agent_state WHERE agent_id=?", (agent_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"agent {agent_id} not found")
        return self._row_to_state(dict(row))

    def update_agent(self, agent_id: str, **kwargs) -> AgentState:
        """局部更新 AgentState 白名单字段（list/dict 自动序列化为 JSON），返回更新后状态。

        Args:
            agent_id: 目标 agent 标识。
            **kwargs: 允许更新的字段，包括 name, description, system, model,
                context_window_limit, message_ids。其中 message_ids 若为 list 则
                自动转为 JSON 字符串。

        Returns:
            更新后的 AgentState 实例。

        注意：
            - 仅允许白名单内的字段，其他字段被忽略。
            - 每个字段单独执行 UPDATE，若多次更新不同字段会产生多次 SQL 调用，
              但本方法设计为一次性更新多个字段的便捷入口，调用方应传入完整字段集。
            - 本方法不更新 memory，因为 memory 由 _row_to_state 动态生成。
        """
        allowed = {"name", "description", "system", "model", "context_window_limit",
                   "message_ids"}
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        for key, val in updates.items():
            # 将 list/dict 序列化为 JSON 字符串以存入数据库
            if isinstance(val, (list, dict)):
                val = json.dumps(val, ensure_ascii=False)
            self.db.execute(f"UPDATE agent_state SET {key}=? WHERE agent_id=?",
                            (val, agent_id))
        return self.get_agent(agent_id)

    def refresh_memory(self, agent_id: str) -> None:
        """从块表重新织入 memory（供 agent 集成作为 system 重编译的挂点）。

        当前 memory 由 get_agent 动态构建（_row_to_state 从 block_manager
        .list_blocks 读最新块），因此这里的重赋值只作用于局部变量；真实的重
        编译逻辑在 agent 集成层接入。

        设计意图：
            - 当块表发生变化（如通过 BlockManager 更新）后，集成层可调用此方法
              触发 memory 刷新，随后重新编译 system prompt。
            - 由于 AgentState 是值对象，此方法仅用于显式标记“需要刷新”，实际
              重编译由外部调用者（如 Agent 循环）根据新 state 执行。
        """
        st = self.get_agent(agent_id)
        st.memory = Memory(blocks=self.block_manager.list_blocks())