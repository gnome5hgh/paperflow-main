"""AgentState：agent 生命周期状态模型（agent_state 表的一行 JSON 快照）。

记录 agent 的元信息、当前记忆块容器与 in-context 窗口的消息 id 列表。
message_ids 是「当前窗口」的持久化——压缩后被驱逐的旧消息只移出该列表、
不删 messages 表行，Recall 完整可追溯。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from paperflow.core.memory.schemas.memory import Memory

__all__ = ["AgentState"]


class AgentState(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)   # Memory 是普通容器类（非 pydantic），需允许任意类型

    agent_id: str
    name: str | None = None
    description: str | None = None
    system: str | None = None
    model: str | None = None
    memory: Memory = Field(default_factory=lambda: Memory(blocks=[]))
    tools: list[Any] = Field(default_factory=list)
    context_window_limit: int | None = None
    message_ids: list[str] = Field(default_factory=list)   # In-context history 窗口消息 id 列表
    created_at: datetime | None = None
