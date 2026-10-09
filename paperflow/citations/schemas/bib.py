"""一条 BibTeX 条目的最小视图。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class BibEntry:
    """一条 BibTeX 条目的最小视图（查找/去重/渲染用）。

    Attributes:
        key: str，引用键（条目唯一编号）
        entry_type: str，条目类型（article/inproceedings/book…）
        title: str，标题字段
        authors: str，author 字段原文
        year: str，year 字段
        fields: dict，全部字段名 → 值
        raw: str，条目原文（@type{...} 到配对右括号，bibtex 渲染用）
    """

    key: str
    entry_type: str = "article"
    title: str = ""
    authors: str = ""
    year: str = ""
    fields: dict = field(default_factory=dict)
    raw: str = ""          # 完整条目原文（含 @type{...}），bibtex 渲染用
