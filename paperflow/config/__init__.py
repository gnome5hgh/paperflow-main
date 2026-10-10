# paperflow/config/__init__.py
"""
全局配置模块，提供 LLM 连接参数和项目运行时配置。

配置加载优先级（从低到高）：
    1. dataclass 默认值（`sections.py` 是所有可调参数值的**唯一声明点**——改默认值只改那里）
    2. config.yaml（可选，文件不存在则跳过；纯覆盖文件，不重复声明默认值）
    3. 环境变量（最高优先级，按 ``PAPERFLOW_`` + 配置路径大写派生）

配置结构与 config.yaml 同构：
``runtime`` / ``corpus`` / ``intent`` / ``rag{embedding, rerank,
retriever, query_rewrite, chunker, storage, tools}`` / ``memory`` /
``session`` / ``agents{timeouts}``，外加保留的顶层 ``llm`` / ``vision`` / ``mcp_servers``。

env 名约定：字段路径以 ``_`` 连接并大写，前缀 ``PAPERFLOW_``。例如
``rag.storage.uri`` → ``PAPERFLOW_RAG_STORAGE_URI``，
``rag.query_rewrite.model`` → ``PAPERFLOW_RAG_QUERY_REWRITE_MODEL``。无例外表；
``agents.timeouts`` 与 ``mcp_servers`` 是自由 dict，仅 YAML 可配（不派生 env）。

使用方式::

    config = PaperFlowConfig.from_env()          # 自动加载
    config = PaperFlowConfig.from_env("my.yaml") # 指定 YAML 路径

包内按角色分两个模块：``sections.py``（配置分区 dataclass 树）+ ``loader.py``
（合并与派生）；本文件只做再导出，消费方一律 ``from paperflow.config import ...``。
"""
from paperflow.config.loader import PaperFlowConfig
from paperflow.config.sections import (
    AgentsConfig,
    ChunkerConfig,
    CorpusConfig,
    EmbeddingConfig,
    IntentConfig,
    IntentJevConfig,
    LLMConfig,
    McpServerConfig,
    MemoryConfig,
    QueryRewriteConfig,
    RagConfig,
    RagToolsConfig,
    RerankConfig,
    RetrieverConfig,
    RuntimeConfig,
    SessionConfig,
    MinioConfig,
    StorageConfig,
    VisionLLMConfig,
    parse_mcp_servers,
)

__all__ = [
    "PaperFlowConfig",
    "LLMConfig",
    "VisionLLMConfig",
    "RuntimeConfig",
    "CorpusConfig",
    "IntentConfig",
    "IntentJevConfig",
    "RagConfig",
    "EmbeddingConfig",
    "RerankConfig",
    "RetrieverConfig",
    "QueryRewriteConfig",
    "ChunkerConfig",
    "MinioConfig",
    "StorageConfig",
    "RagToolsConfig",
    "MemoryConfig",
    "SessionConfig",
    "AgentsConfig",
    "McpServerConfig",
    "parse_mcp_servers",
]
