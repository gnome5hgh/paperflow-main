"""LLM 领域模型：wire 消息家族与组件行为参数。

只放「数据长什么样」的对象；客户端与结构化抽取是服务，在 `../services/`。
"""
from paperflow.core.llm.domain.dto import (
    AssistantMessage,
    Message,
    ResponseMeta,
    StructuredOutputConfig,
    SystemMessage,
    ToolMessage,
    Usage,
    UserMessage,
    message_from_role,
)

__all__ = [
    "Message",
    "SystemMessage",
    "UserMessage",
    "AssistantMessage",
    "ToolMessage",
    "ResponseMeta",
    "Usage",
    "message_from_role",
    "StructuredOutputConfig",
]
