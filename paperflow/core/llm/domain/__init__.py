"""LLM 领域模型：wire 消息家族。

只放「数据长什么样」的对象；客户端与结构化抽取是服务，在 `../services/`，
组件行为参数在 `../config/`。
"""
from paperflow.core.llm.domain.dto import (
    AssistantMessage,
    Message,
    ResponseMeta,
    SystemMessage,
    ToolMessage,
    Usage,
    UserMessage,
    message_from_role,
    response_meta_from_dict,
    response_meta_to_dict,
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
    "response_meta_to_dict",
    "response_meta_from_dict",
]
