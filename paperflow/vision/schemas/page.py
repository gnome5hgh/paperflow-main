"""一页文本的解析结果：页码 + 按阅读顺序排列的段落。"""
from dataclasses import dataclass

from paperflow.vision.common.geometry import Paragraph


@dataclass(frozen=True)
class Page:
    """一页文本:页码 + 按阅读顺序排列的段落列表。

    Attributes:
        page_number: int，页码（0 起，与 fitz 一致）
        paragraphs: list[Paragraph]，按阅读顺序排列的段落
    """

    page_number: int
    paragraphs: list[Paragraph]

