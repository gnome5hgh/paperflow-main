"""LLM 领域模型：wire 消息契约。

只放「数据长什么样」的对象；客户端与结构化抽取是服务，在 `../services/`。
"""
from paperflow.core.llm.domain.dto import Message

__all__ = ["Message"]
