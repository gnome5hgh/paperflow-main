# paperflow/core/llm/__init__.py
"""LLM 域包 —— wire 消息家族、对话客户端与结构化输出。

公共出口（下游一律 `from paperflow.core.llm import ...`）：
- wire 消息家族 ← domain/dto/messages：基类 ``Message``，按角色分的
  ``SystemMessage`` / ``UserMessage`` / ``AssistantMessage`` / ``ToolMessage``，
  以及从角色字符串还原类型的 ``message_from_role``（持久化回放用）
- ``StructuredOutputConfig``（结构化输出的行为参数）← domain/dto
- ``LLMClient`` / ``tool_to_openai_schema`` / ``StructuredOutput`` /
  ``StructuredOutputError`` ← services

分层：``domain/`` 放消息契约与组件参数，``services/`` 放客户端与结构化抽取——是
**服务**而非工具集，故与 `common/`（横向共用的小叶子）分开。

**这里只有 LLM 客户端**：稠密编码与精排的协议与实现在 `paperflow.core.rag.encoders`
（只服务 RAG）。私有辅助（``_message_to_openai`` / ``_accumulate_stream_chunks`` /
``_extract_json_body`` 等）也不在此导出，需要时从具体子模块 import。
"""
from paperflow.core.llm.domain import (
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
from paperflow.core.llm.services import (
    LLMClient,
    StructuredOutput,
    StructuredOutputError,
    tool_to_openai_schema,
)

__all__ = [
    "LLMClient",
    "Message",
    "SystemMessage",
    "UserMessage",
    "AssistantMessage",
    "ToolMessage",
    "ResponseMeta",
    "Usage",
    "message_from_role",
    "StructuredOutput",
    "StructuredOutputConfig",
    "StructuredOutputError",
    "tool_to_openai_schema",
]
