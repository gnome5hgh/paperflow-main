"""RAG 领域模型：实体与层间数据载体，是 rag 各层的公共词汇。

按后端分层惯例分两个子包：

- ``entity/`` —— 领域实体与值对象：RAG 业务里「一个东西」的模型（检索块 Chunk、章节 Section）
- ``dto/``    —— 层间传递的数据载体：函数/服务之间搬运的入参与结果对象
  （解析产物 PdfText/Block、索引结果 IndexOutcome/IndexRunOutcome/IndexStatus、
  查询改写结果 RewriteResult）

消费方一律从本包取（``from paperflow.rag.domain import Chunk``），不关心内部怎么分文件。
这些模型只依赖 ``rag.constants`` 的取值词汇，不依赖任何服务实现，因此可被任意层安全导入，
不会反向拖入 pymilvus / jieba 等重依赖。
"""
from paperflow.rag.domain.dto import (
    Block,
    IndexOutcome,
    IndexRunOutcome,
    IndexStatus,
    PdfText,
    RewriteResult,
)
from paperflow.rag.domain.entity import (
    Chunk,
    Section,
    context_prefix,
    indexed_text,
    section_label,
)

__all__ = [
    # entity
    "Chunk",
    "Section",
    "context_prefix",
    "indexed_text",
    "section_label",
    # dto
    "Block",
    "PdfText",
    "IndexOutcome",
    "IndexRunOutcome",
    "IndexStatus",
    "RewriteResult",
]
