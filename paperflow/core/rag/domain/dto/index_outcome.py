"""单篇文档入库结果 dto。"""
from dataclasses import dataclass

from paperflow.core.rag.constants import IndexOutcomeStatus


@dataclass
class IndexOutcome:
    """单篇文档的入库结果。

    Attributes:
        status: IndexOutcomeStatus，入库结局（已入库 / 未入库 / 旧块清了但切不出块）
        chunks: int，本次写入的块数（仅 INDEXED 有意义）
        reason: str | None，跳过或空结果的原因，供调用方如实转述
    """
    status: IndexOutcomeStatus
    chunks: int = 0
    reason: str | None = None
