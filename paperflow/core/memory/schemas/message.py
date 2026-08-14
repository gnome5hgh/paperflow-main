"""Recall 持久化消息模型：Message / MessageRole。

与 paperflow/core/llm.py::Message（OpenAI wire 格式）区分：本类型是落盘到
messages 表的持久化消息，由 MessageManager 从 wire 格式转换生成，补上 id /
created_at 等存储字段。content 恒为字符串（str | None），落盘与回放都不做
JSON 类型猜测。
"""
from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

__all__ = ["Message", "MessageRole"]


class MessageRole(Enum):
    """对话消息的角色枚举（与 OpenAI wire 的角色名一致）。"""

    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class Message(BaseModel):
    """持久化消息：含 Recall 检索所需的全部字段。

    tool_calls / tool_call_id 记录工具调用轨迹；step_id / run_id / otid 是
    审计与轨迹追踪的关联键。
    """

    id: str = Field(default_factory=lambda: f"message-{uuid.uuid4().hex}")
    role: MessageRole
    content: str | None = None
    tool_calls: list[dict] = Field(default_factory=list)
    tool_call_id: str | None = None
    step_id: str | None = None
    run_id: str | None = None
    otid: str | None = None
    created_at: datetime | None = None
