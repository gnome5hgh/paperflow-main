"""RAG 域的跨模块词汇。

块 id 长度与块类型取值在切块与索引两处流转，入库结果状态在索引器与工具层之间
流转，都是这个包收的「跨文件共享契约」；本包只做再导出，消费方一律从
`paperflow.core.rag.constants` 取，不关心内部怎么分文件。
"""
from paperflow.core.rag.constants.constants import (
    CHUNK_ID_LEN,
    CHUNK_TYPE_FIGURE,
    CHUNK_TYPE_TABLE,
    CHUNK_TYPE_TEXT,
)
from paperflow.core.rag.constants.enums import IndexOutcomeStatus

__all__ = [
    "CHUNK_ID_LEN",
    "CHUNK_TYPE_FIGURE",
    "CHUNK_TYPE_TABLE",
    "CHUNK_TYPE_TEXT",
    "IndexOutcomeStatus",
]
