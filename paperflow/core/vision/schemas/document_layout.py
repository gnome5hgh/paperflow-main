"""文档级版面统计结果（字段照 DocumentLayout.scala）。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class DocumentLayout:
    """文档级统计结果（字段照 DocumentLayout.scala）。

    Attributes:
        two_columns: 正文是否双栏（存在两簇用量相近、相距足够远的左边距）。
        standard_font_size: 最常见字号；未过半即认为无主导字号，置 None。
        standard_width_bucketed: 最常见行宽（2pt 桶粒度）；占比不足时置 None。
        standard_width: 最常见行宽（未分桶的真实值）；占比不足时置 None。
        average_word_spacing: 词间距均值（只统计间距为正的词对）。
        trust_left_margin: 左边距分布是否可信（可信才可用于边距启发式）。
        left_margins: 各左边距（x1 取整）→ 占全文字数的比例。
        font_counts: 各字体名 → 出现字符数（原生计数，非比例）。
        median_line_spacing: 正文行距的加权中位数（只统计水平重叠的正间距行对）。
    """

    two_columns: bool
    standard_font_size: float | None
    standard_width_bucketed: float | None
    standard_width: float | None
    average_word_spacing: float
    trust_left_margin: bool
    left_margins: dict[int, float]
    font_counts: dict[str, int]
    median_line_spacing: float

