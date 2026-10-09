# paperflow/core/llm/__init__.py
"""LLM 域包 —— wire 消息契约、对话客户端与结构化输出。

公共出口（下游一律 `from paperflow.core.llm import ...`）：
- ``Message``（wire 格式消息）/ ``LLMClient`` / ``tool_to_openai_schema`` ← client
- ``StructuredOutput`` / ``StructuredOutputConfig`` / ``StructuredOutputError`` ← structured

**这里只有 LLM 客户端**：稠密编码与精排的协议与实现在 `paperflow.rag.models`
（只服务 RAG）。私有辅助（``_message_to_openai`` / ``_accumulate_stream_chunks`` /
``_extract_json_body`` 等）也不在此导出，需要时从具体子模块 import。
"""

from paperflow.core.llm.client import LLMClient, Message, tool_to_openai_schema
from paperflow.core.llm.structured import (
    StructuredOutput,
    StructuredOutputConfig,
    StructuredOutputError,
)

__all__ = [
    "LLMClient",
    "Message",
    "StructuredOutput",
    "StructuredOutputConfig",
    "StructuredOutputError",
    "tool_to_openai_schema",
]
