"""Letta Message / MessageRole 持久化模型，命名对齐 letta/schemas/message.py。

与 paperflow/core/llm.py::Message（OpenAI wire 格式）区分：本类型是 Recall
持久化消息，由 MessageManager 落盘时从 wire 格式转换生成。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Message", "MessageRole"]


class MessageRole(Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class Message(BaseModel):
    id: str = Field(default_factory=lambda: f"message-{uuid.uuid4().hex}")
    role: MessageRole
    content: str | None = None
    tool_calls: list[dict] = Field(default_factory=list)
    tool_call_id: str | None = None
    step_id: str | None = None
    run_id: str | None = None
    otid: str | None = None
    created_at: datetime | None = None
