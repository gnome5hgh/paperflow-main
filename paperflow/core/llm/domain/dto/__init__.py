"""LLM 域的层间数据载体。

``Message`` 是请求侧的 wire 消息契约，``LLMResponse`` 是模型回复（同一字段形状 +
回复才有的截断标记），``StructuredOutputConfig`` 是结构化输出组件的行为参数。
schema → prompt 渲染与结构化抽取本身是**服务**（``services/structured.py``），
不放这里。
"""
from paperflow.core.llm.domain.dto.message import Message
from paperflow.core.llm.domain.dto.response import LLMResponse
from paperflow.core.llm.domain.dto.structured_output_config import StructuredOutputConfig

__all__ = ["LLMResponse", "Message", "StructuredOutputConfig"]
