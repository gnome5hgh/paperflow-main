"""LLM 域的层间数据载体。

目前只有 ``Message``——wire 消息契约。schema → prompt 渲染与结构化抽取是**服务**
（``services/structured.py``），不放这里。
"""
from paperflow.core.llm.domain.dto.message import Message

__all__ = ["Message"]
