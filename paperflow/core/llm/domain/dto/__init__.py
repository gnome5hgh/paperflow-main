"""LLM 域的层间数据载体。

``Message`` 家族是 wire 消息契约——**进出两个方向共用它**（`chat` 返回的就是一条
assistant 消息，下一轮原样回放为请求消息）。schema → prompt 渲染与结构化抽取
是**服务**（``services/structured.py``），不放这里。
"""
from paperflow.core.llm.domain.dto.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
    message_from_role,
)
from paperflow.core.llm.domain.dto.structured_output_config import StructuredOutputConfig

__all__ = [
    "Message",
    "SystemMessage",
    "UserMessage",
    "AssistantMessage",
    "ToolMessage",
    "message_from_role",
    "StructuredOutputConfig",
]
