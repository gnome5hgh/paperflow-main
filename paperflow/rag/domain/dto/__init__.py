"""RAG 层间数据载体（dto）。

把数据从一层搬到另一层的对象：解析产物（PdfText / Block）、索引结果
（IndexOutcome / IndexRunOutcome / IndexStatus）、查询改写结果（RewriteResult）。
与 `entity/` 的分界是「是不是 RAG 业务里的东西本身」——这里只回答「数据长什么样」。
"""
from paperflow.rag.domain.dto.block import Block
from paperflow.rag.domain.dto.index_outcome import IndexOutcome
from paperflow.rag.domain.dto.index_run_outcome import IndexRunOutcome
from paperflow.rag.domain.dto.index_status import IndexStatus
from paperflow.rag.domain.dto.pdf_text import PdfText
from paperflow.rag.domain.dto.rewrite_result import RewriteResult

__all__ = [
    "Block",
    "PdfText",
    "IndexOutcome",
    "IndexRunOutcome",
    "IndexStatus",
    "RewriteResult",
]
