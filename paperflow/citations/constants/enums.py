"""引用域的枚举——解析状态与删除结局。"""

from enum import StrEnum

__all__ = ["CitationStatus", "RemoveOutcome"]


class CitationStatus(StrEnum):
    """一条引用解析到语料库的结果。

    IN_CORPUS 只说明标题在语料索引里命中，不代表已入库 references.bib——
    「是否已落地」由 ResolvedCitation.in_bib 单独表达，两者不可互换。
    """

    IN_CORPUS = "in_corpus"
    MISSING = "missing"


class RemoveOutcome(StrEnum):
    """删除一条引用的结局。

    多条命中时不猜（误删是单向的），返回 AMBIGUOUS 连同候选交用户选择。
    """

    REMOVED = "removed"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
