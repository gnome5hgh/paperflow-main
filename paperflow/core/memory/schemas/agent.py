"""Letta AgentState 模型，命名对齐 letta/schemas/agent.py。"""
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
    message_ids: list[str] = Field(default_factory=list)   # Message Buffer (In-context history)
    created_at: datetime | None = None
