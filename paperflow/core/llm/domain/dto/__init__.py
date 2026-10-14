"""LLM 域的层间数据载体。

``Message`` 是 wire 消息契约——**进出两个方向共用它**（`chat` 返回的就是一条
assistant Message，下一轮原样回放为请求消息）。schema → prompt 渲染与结构化抽取
是**服务**（``services/structured.py``），不放这里。
"""
from paperflow.core.llm.domain.dto.message import Message
from paperflow.core.llm.domain.dto.structured_output_config import StructuredOutputConfig

__all__ = ["Message", "StructuredOutputConfig"]
