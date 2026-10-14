"""一条引用解析到语料库的结果。"""

from enum import StrEnum

__all__ = ["CitationStatus"]


class CitationStatus(StrEnum):
    """一条引用解析到语料库的结果。

    IN_CORPUS 只说明标题在语料索引里命中，不代表已入库 references.bib——
    「是否已落地」由 ResolvedCitation.in_bib 单独表达，两者不可互换。
    """

    IN_CORPUS = "in_corpus"
    MISSING = "missing"
