"""语料来源类型枚举。"""

from enum import StrEnum

__all__ = ["RagSource"]


class RagSource(StrEnum):
    """一个检索块来自哪类语料：读书笔记还是论文原文。"""

    NOTE = "note"
    PDF = "pdf"
