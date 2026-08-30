"""文档级布局统计：双栏检测、标准字号/行宽、词距、左边距信任度、中位行距。

pdffigures2 的 DocumentLayout.scala 移植。CaptionDetector（Task 13）用它做字体过滤、
RegionClassifier（Task 15）做字号/间距/边距分类、FigureDetector（Task 16）做双栏中心线
判断，故统计口径与 Scala 逐项一致，常量照抄。

输入是 text_extractor 抽出的 Page（word/line/paragraph 结构），信息不足（词距/字体/
行距/行宽任一维度没有样本）时返回 None，避免把缺文本文档误判成某种布局。
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from paperflow.vision.parsers.text_extractor import Page

# 行宽分桶粒度(pt)：把相近行宽聚到 2pt 桶里，弱化浮动对象/公式造成的宽度噪声
LINE_WIDTH_BUCKET_SIZE = 2

# 双栏判定：两簇左边距「用量差」相对值上限（相近才算双栏，否则只是一栏加少量噪声）+ x1 距离下限
_TWO_COLUMN_MAX_USAGE_DIFFERENCE = 0.40
_TWO_COLUMN_MAX_X_DIFFERENCE = 0.40

# 标准行宽：最常见宽度的用量占比须超过该比例才可信（否则宽度分布太散，没有主导行宽）
_MIN_COMMON_LINE_WIDTH_USE = 0.4

# 左边距信任度：顶部若干边距的累计占比须超过阈值才认为边距分布可信；
# 双栏时左右各一个主导边距，故多取一倍边距数再求和
_TRUST_MARGINS_TWO_COLUMN_THRESHOLD = 0.65
_TRUST_MARGINS_NUM_MARGINS_TO_COUNT = 3
_TRUST_MARGINS_ONE_COLUMN_THRESHOLD = 0.55


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


def _weighted_median(inputs: list[tuple[float, int]]) -> float:
    """加权中位数：反复剔除「累计移走权重更小」的一端，直到只剩一个值。

    权重只决定每次剔除哪一端，最终取值就是存活 (value, weight) 的值本身
    （照 DocumentLayout.scala 的 weightedMedian 逐逻辑移植）。
    """
    sorted_inputs = sorted(inputs, key=lambda p: p[0])
    removed_from_start = 0
    removed_from_end = 0
    while len(sorted_inputs) != 1:
        if (
            removed_from_end + sorted_inputs[-1][1]
            < removed_from_start + sorted_inputs[0][1]
        ):
            removed_from_end += sorted_inputs[-1][1]
            sorted_inputs = sorted_inputs[:-1]
        else:
            removed_from_start += sorted_inputs[0][1]
            sorted_inputs = sorted_inputs[1:]
    return sorted_inputs[0][0]


def build_document_layout(pages: list[Page]) -> DocumentLayout | None:
    """统计整篇文档的布局信息；信息不足（词距/字体/行距/行宽任一维度无样本）返回 None。

    对应 Scala 的 object DocumentLayout.apply(textPages)——Python 中 dataclass 构造器
    无法承载这个工厂，故用独立函数，语义与名字无关。逐行累计四类统计：左边距分布
    （行 x1 取整）、行宽（2pt 桶 + 真实值）、词间距、行距（水平重叠的正间距行对）；
    最后算字号众数、行宽众数、加权中位行距、双栏判定、左边距信任度。
    """
    total_word_spacing = 0.0
    total_word_spaces = 0
    left_margins: dict[int, int] = {}
    font_counts: dict[str, int] = {}
    line_widths: dict[float, int] = {}  # 2pt 桶粒度行宽计数（每行计两次，见下）
    raw_line_widths: dict[float, int] = {}  # 未分桶的真实行宽计数（每行计一次）
    font_size_counts: dict[float, int] = {}
    line_spacing: list[tuple[float, int]] = []

    for text_page in pages:
        prev_line_bb = None  # 上一水平行包围盒（跨段落延续，页面首行重置——照 Scala）
        for paragraph in text_page.paragraphs:
            for line in paragraph.lines:
                if not line.is_horizontal:
                    continue  # 竖直/旋转文本不参与行布局统计（照 Scala 的 filter isHorizontal）
                weight = len(line.words)
                x1 = round(line.boundary.x1)
                left_margins[x1] = left_margins.get(x1, 0) + weight
                if prev_line_bb is not None:
                    bb = line.boundary
                    space = bb.y1 - prev_line_bb.y2
                    # 只有与上一行水平重叠且确有正间距的行对才计入行距：
                    # 同一栏内相邻行才代表真实行距，跨栏/换栏处的空隙不算
                    if prev_line_bb.x1 < bb.x2 and prev_line_bb.x2 > bb.x1 and space > 0:
                        line_spacing.append((space, weight))
                w = line.boundary.width
                lower_bucket = math.floor(w / LINE_WIDTH_BUCKET_SIZE) * LINE_WIDTH_BUCKET_SIZE
                upper_bucket = math.ceil(w / LINE_WIDTH_BUCKET_SIZE) * LINE_WIDTH_BUCKET_SIZE
                # 行宽同时计入上下两个桶：行宽落在桶边界附近时两头都算，防边界抖动
                line_widths[lower_bucket] = line_widths.get(lower_bucket, 0) + weight
                line_widths[upper_bucket] = line_widths.get(upper_bucket, 0) + weight
                raw_w = round(w, 2)
                raw_line_widths[raw_w] = raw_line_widths.get(raw_w, 0) + weight

                prev_line_bb = line.boundary
                prev_word = None
                for word in line.words:
                    if prev_word is not None:
                        spacing = word.boundary.x1 - prev_word.boundary.x2
                        if spacing > 0:  # 只统计正间距，重叠/贴行的词对不计
                            total_word_spacing += spacing
                            total_word_spaces += 1
                    for pos in word.positions:
                        font_counts[pos.font_name] = font_counts.get(pos.font_name, 0) + 1
                        font_size_counts[pos.font_size] = (
                            font_size_counts.get(pos.font_size, 0) + 1
                        )
                    prev_word = word

    total_chars = sum(font_counts.values())
    total_lines = sum(line_widths.values()) / 2  # 每行计了两次，除 2 得按词加权的真实行数

    if total_word_spaces == 0 or total_chars == 0 or not line_spacing or total_lines == 0:
        # 信息不足：通常是文本几乎抽不出来（扫描件/纯图文档），给不出可信布局
        return None

    most_common_font_size, most_common_font_size_count = max(
        font_size_counts.items(), key=lambda kv: kv[1]
    )
    # 字号众数超过半数才承认「标准字号」，否则混排文档没有统一正文号
    standard_font_size = (
        most_common_font_size if most_common_font_size_count > total_chars / 2.0 else None
    )

    median_line_spacing = _weighted_median(line_spacing)

    most_common_width, most_common_width_count = max(
        line_widths.items(), key=lambda kv: kv[1]
    )
    standard_width_bucketed = (
        most_common_width
        if most_common_width_count > total_lines * _MIN_COMMON_LINE_WIDTH_USE
        else None
    )
    most_common_raw_width, most_common_raw_count = max(
        raw_line_widths.items(), key=lambda kv: kv[1]
    )
    standard_width = (
        most_common_raw_width
        if most_common_raw_count > total_lines * _MIN_COMMON_LINE_WIDTH_USE
        else None
    )

    sorted_left_margins = sorted(left_margins.items(), key=lambda kv: -kv[1])
    top2 = sorted_left_margins[:2]
    most_common = top2[0]
    second_most_common = top2[1] if len(top2) > 1 else None
    two_columns = False
    if second_most_common is not None:
        # 两簇左边距「用量差」相对值要小（两栏正文量接近）、x1 距离要远（确实分居两侧）
        diff = abs(most_common[1] - second_most_common[1]) / (
            most_common[1] + second_most_common[1]
        )
        two_columns = (
            diff < _TWO_COLUMN_MAX_USAGE_DIFFERENCE
            and abs(most_common[0] - second_most_common[0])
            > _TWO_COLUMN_MAX_X_DIFFERENCE
        )

    total_margin_counts = sum(left_margins.values())
    sorted_margins_by_percent = [
        (margin, count / total_margin_counts) for margin, count in sorted_left_margins
    ]
    if two_columns:
        # 双栏时左右各一个主导边距，多取一倍边距数再求和判断
        words_in_top_margins = sum(
            p for _, p in sorted_margins_by_percent[: _TRUST_MARGINS_NUM_MARGINS_TO_COUNT * 2]
        )
        trust_left_margin = words_in_top_margins > _TRUST_MARGINS_TWO_COLUMN_THRESHOLD
    else:
        words_in_top_margins = sum(
            p for _, p in sorted_margins_by_percent[:_TRUST_MARGINS_NUM_MARGINS_TO_COUNT]
        )
        trust_left_margin = words_in_top_margins > _TRUST_MARGINS_ONE_COLUMN_THRESHOLD

    return DocumentLayout(
        two_columns=two_columns,
        standard_font_size=standard_font_size,
        standard_width_bucketed=standard_width_bucketed,
        standard_width=standard_width,
        average_word_spacing=total_word_spacing / total_word_spaces,
        trust_left_margin=trust_left_margin,
        left_margins=dict(sorted_margins_by_percent),
        font_counts=font_counts,
        median_line_spacing=median_line_spacing,
    )
