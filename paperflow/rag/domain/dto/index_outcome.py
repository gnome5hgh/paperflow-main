"""单篇文档入库结果 dto。"""
from dataclasses import dataclass


@dataclass
class IndexOutcome:
    """单篇文档的入库结果。

    Attributes:
        status: str，"indexed"（已入库）/ "skipped"（未入库：文件不存在或不在语料根下）
            / "empty"（旧块已清理但新内容切不出块）
        chunks: int，本次写入的块数（仅 "indexed" 有意义）
        reason: str | None，跳过或空结果的原因，供调用方如实转述
    """
    status: str
    chunks: int = 0
    reason: str | None = None
