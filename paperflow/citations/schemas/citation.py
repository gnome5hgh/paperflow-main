"""引用解析结果。"""
from __future__ import annotations

from dataclasses import dataclass

from paperflow.citations.constants import CitationStatus


@dataclass
class ResolvedCitation:
    """引用解析结果：key + 语料状态 + PDF 路径。

    溯源只跟踪 PDF：笔记是 agent 自己的产物，不参与引用解析。

    Attributes:
        key: BibTeX 条目的键，若 status=MISSING 则为 None。
        status: CitationStatus，IN_CORPUS（语料库内）或 MISSING（库外/未找到）。
        title: 论文全标题。
        year: 发表年份。
        pdf_path: 关联的 PDF 文件路径（若有）。
    """

    key: str | None
    status: CitationStatus
    title: str = ""
    year: str = ""
    pdf_path: str | None = None
    #: key 是否已落地在 references.bib（bib 真相源）。in_corpus 只说明语料标题
    #: 索引命中——key 可能是现场生成、尚未入库的（溯源链断裂的根因）。in_bib=False
    #: 时标注 [来源:key§节] 属于无据声称，必须先
    #: add_citation 成功或降级为 [⚠未入库]。
    in_bib: bool = False
