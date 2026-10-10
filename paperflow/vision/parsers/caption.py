"""图注检测与扩展：CaptionDetector（正则识别起始行 + filter sieve 消歧）+ CaptionBuilder（起始行向后扩展成完整图注）。

pdffigures2 的 CaptionDetector.scala + CaptionBuilder.scala + Figure.scala（Caption 部分）移植。

- find_captions: 用正则找出所有可能以「Figure 1 / Fig. 3 / Table 1」开头的行
  （通常含大量误报，如正文里 "Figure 2 shows..." 的引用），再用一组格式过滤器
  （filter sieve）按同图号分组逐轮消歧——只保留格式一致的候选。最后的保底是
  段落首行判别；仍无法消歧的图号（同图号候选 >3 或同页 >2）整体放弃。
- build_captions: 从每个图注起始行出发，按行距/字体/对齐等启发式向后合并后续行，
  扩展成完整图注段落。

图注类型 FigureType ∈ {Figure, Table}，由起始词首字母判定：'F' → 图，否则 → 表。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

from paperflow.vision.common.geometry import Box, Box_container, Line, Paragraph
from paperflow.vision.constants import FigureType
from paperflow.vision.schemas.caption import Caption, CaptionParagraph, CaptionStart
from paperflow.vision.schemas.page import Page


# ---------------------------------------------------------------------------
# CaptionDetector：正则识别起始行 + filter sieve 消歧
# ---------------------------------------------------------------------------

# 消歧「放弃」门槛：同一 (图型, 图号) 前缀出现 4+ 个候选、或同页出现 3+ 个，
# 且无法继续裁剪时放弃该前缀（照 CaptionDetector.scala）
MaxDuplicateCaptionNames = 3
MaxSamePageDuplicateCaptionNames = 2

# 图注起始行高度上限：极少数 PDF 文本抽取异常会把整段塞进单行（高度虚高），
# 这类行不参与图注识别
MaxHeightForCaptionLines = 60

# 常见字体占比门槛：最常见字体须超过该比例才启用 NonStandardFont 过滤器
MinCommonFontPercentage = 0.4

# 图注起始词（原 Scala 正则原样保留，含那个空分支——空分支只会匹配空串，无害）
_CAPTION_START_RE = re.compile(
    r"^(Figure.|Figure|FIGURE|Table|TABLE||Fig.|Fig|FIG.|FIG)$"
)

# 图注图号：支持 "3.1"/"3"/"III"/"1a"/"A.1" 等写法，后随 ':'、'.' 或行尾。
# 注意两个 '.' 都是未转义的「任意字符」，照抄原实现——这样 "3-1" 这类分隔也能命中；
# 匹配到词中的图号即停，尾随说明文字留给下一个词
_CAPTION_NUMBER_RE = re.compile(
    r"^([1-9][0-9]*.[1-9][0-9]*|[1-9][0-9]*|[IVX]+|[1-9I][0-9I]*|[A-D].[1-9][0-9]*)($|:|.)?"
)


def find_caption_candidates(pages: list[Page]) -> list[CaptionStart]:
    """找出所有可能是图注起始行的候选（含误报，消歧交给 select_caption_candidates）。

    Args:
        pages: 所有页的文本结构（Page 列表）。

    Returns:
        原始的 CaptionStart 候选列表，可能包含大量误报（如正文中的引用）。

    算法：
        逐段逐行：
        1. 检查第一个词是否匹配图注起始词表（_CAPTION_START_RE）。
        2. 若匹配，取第二个词（若第一个词是 "Fig" 后跟 "."，则将 "Fig" + "." 合并作为起始词，图号为第三个词）。
        3. 第二个词（或第三个）是否匹配图号正则（_CAPTION_NUMBER_RE）。
        4. 行高需小于 MaxHeightForCaptionLines（排除异常行）。
        5. 记录 line_end、paragraph_start 等特征供后续过滤器使用。
    """
    candidates: list[CaptionStart] = []
    for page in pages:
        for paragraph in page.paragraphs:
            paragraph_start = True
            for line_num, line in enumerate(paragraph.lines):
                first_word = line.words[0].text
                # PDFBox 偶尔把 "Fig." 拆成 "Fig" + "." 两个词，这里把两者合回再识别
                if len(line.words) > 2 and line.words[1].text == ".":
                    header_str = first_word + "."
                    word_number = 2
                else:
                    header_str = first_word
                    word_number = 1
                start_match = _CAPTION_START_RE.match(first_word)
                if start_match and len(line.words) > 1:
                    number_str = line.words[word_number].text
                    number_match = _CAPTION_NUMBER_RE.match(number_str)
                    # 行高超过上限说明文本抽取异常（整段被塞进单行），不作为图注起始
                    if number_match and line.boundary.height < MaxHeightForCaptionLines:
                        next_line = (
                            paragraph.lines[line_num + 1]
                            if line_num + 1 < len(paragraph.lines)
                            else None
                        )
                        candidates.append(
                            CaptionStart(
                                header_str,
                                number_match.group(1),
                                FigureType.Figure
                                if start_match.group(1)[0] == "F"
                                else FigureType.Table,
                                number_match.group(2) or "",
                                line,
                                next_line,
                                page.page_number,
                                paragraph_start,
                                number_match.end() == len(number_str)
                                and len(line.words) == word_number + 1,
                            )
                        )
                paragraph_start = False
    return candidates


# ---- 以下为各个过滤器函数（每个返回 bool，True 表示保留该候选） ----
# 过滤器按顺序组成一个 sieve，每轮挑一个可裁掉一些候选但不会整组裁掉的过滤器应用。

def _colon_only(cc: CaptionStart) -> bool:
    """保留图号后跟冒号的候选（如 "Figure 1:"）。

    Args:
        cc: CaptionStart，待过滤候选

    Returns:
        True 表示保留（图号后跟冒号）。
    """
    return cc.colon_match


def _all_caps_fig_only(cc: CaptionStart) -> bool:
    """保留全大写 FIG 开头的图注；表注不受此限制。

    Args:
        cc: CaptionStart，待过滤候选

    Returns:
        True 表示保留（全大写 FIG 形式）。
    """
    return cc.all_caps_fig or cc.fig_type == FigureType.Table


def _all_caps_table_only(cc: CaptionStart) -> bool:
    """保留全大写 TABLE 的表注；图注不受此限制。

    Args:
        cc: CaptionStart，待过滤候选

    Returns:
        True 表示保留（全大写 TABLE 形式）。
    """
    return cc.all_caps_table or cc.fig_type == FigureType.Figure


def _non_standard_font(
    standard_font: str, types: set[FigureType]
) -> Callable[[CaptionStart], bool]:
    """构造 NonStandardFont 过滤器：被过滤图型若首字符用的不是标准字体则剔除。

    Args:
        standard_font: 文档中最常见的字体名。
        types: 需要应用此过滤器的图注类型集合。

    Returns:
        过滤器函数，接受 CaptionStart 返回 bool。
    """
    def accept(cc: CaptionStart) -> bool:
        """字体过滤器：目标类型的图注首字符字体非标准字体则剔除。

        Args:
            cc: CaptionStart，待过滤候选

        Returns:
            True 表示保留。
        """
        return (
            cc.fig_type not in types
            or cc.line.words[0].positions[0].font_name != standard_font
        )
    return accept


def _abbreviated_fig_only(cc: CaptionStart) -> bool:
    """保留缩写 "Fig." 的图注；表注不受限制。

    Args:
        cc: CaptionStart，待过滤候选

    Returns:
        True 表示保留（缩写 Fig.，或表注不受限）。
    """
    return cc.fig_abbreviated or cc.fig_type == FigureType.Table


def _figure_has_following_text_only(cc: CaptionStart) -> bool:
    """图注要求图号词不在行尾（即后面有正文）；表注直接放行。

    Args:
        cc: CaptionStart，待过滤候选

    Returns:
        True 表示保留（图号不在行尾，或表注直接放行）。
    """
    return cc.fig_type == FigureType.Table or not cc.line_end


def _period_only(cc: CaptionStart) -> bool:
    """保留图号后跟句点的候选（如 "Figure 3."）。

    Args:
        cc: CaptionStart，待过滤候选

    Returns:
        True 表示保留（图号后跟句点）。
    """
    return cc.period_match


def _left_aligned_only(figure_only: bool) -> Callable[[CaptionStart], bool]:
    """构造左对齐过滤器：图注起始行与下一行左缘对齐（±1pt）才放行。

    Args:
        figure_only: 若为 True，则只对 Figure 类型应用此过滤，Table 直接放行。
    """
    def accept(cc: CaptionStart) -> bool:
        """左对齐过滤器：起始行与下一行左缘对齐（±1pt）才放行。

        Args:
            cc: CaptionStart，待过滤候选

        Returns:
            True 表示保留（无下一行时放行）。
        """
        if figure_only and cc.fig_type == FigureType.Table:
            return True
        if cc.next_line is None:
            return True
        return abs(cc.line.boundary.x1 - cc.next_line.boundary.x1) < 1
    return accept


def _line_end_only(cc: CaptionStart) -> bool:
    """保留图号词在行尾的候选。

    Args:
        cc: CaptionStart，待过滤候选

    Returns:
        True 表示保留（图号词在行尾）。
    """
    return cc.line_end


def select_caption_candidates(
    candidates: list[CaptionStart],
    filters: list[tuple[str, Callable[[CaptionStart], bool]]],
) -> list[CaptionStart]:
    """按过滤器逐轮裁剪重复的 (图型, 图号) 候选组。

    Args:
        candidates: 所有候选。
        filters: (过滤器名称, 过滤器函数) 列表，按顺序尝试。

    Returns:
        消歧后的候选列表（每个 (图型,图号) 组内最多保留一个候选，若无法消歧则整组放弃）。

    算法（filter sieve）：
        1. 按 (fig_type, name) 分组。
        2. 反复迭代：
           a. 遍历 filters，找第一个满足「能裁掉至少一个候选」且「不会把整组都裁掉」的过滤器。
           b. 若找到，则应用该过滤器，从每组中剔除不满足的候选。
           c. 若找不到这样的过滤器，则退回到「段落首行」判别：保留 paragraph_start=True 的候选；
              若这样能减少候选数，则继续循环；否则停止。
        3. 最后，对于每个组，若组内候选数 > MaxDuplicateCaptionNames 或同一页内候选数 > MaxSamePageDuplicateCaptionNames，
           则整个组放弃；否则保留组内所有候选（通常此时只剩一个）。
    """
    grouped_by_id: dict[tuple[FigureType, str], list[CaptionStart]] = {}
    for c in candidates:
        grouped_by_id.setdefault((c.fig_type, c.name), []).append(c)

    removed_any = True
    while removed_any and any(len(v) > 1 for v in grouped_by_id.values()):
        filter_to_use = None
        for name, accept in filters:
            # 检查是否有组能因此裁掉至少一个候选（removes_any）
            removes_any = any(
                any(not accept(c) for c in group) for group in grouped_by_id.values()
            )
            # 检查是否有组会被全部裁掉（removes_group），这种情况要避免
            removes_group = any(
                all(not accept(c) for c in group) for group in grouped_by_id.values()
            )
            if removes_any and not removes_group:
                filter_to_use = (name, accept)
                break
        if filter_to_use is not None:
            _, accept = filter_to_use
            grouped_by_id = {
                fig_id: [c for c in group if accept(c)]
                for fig_id, group in grouped_by_id.items()
            }
        else:
            # 格式过滤器都无能为力时，退回用段落首行判别（PDFBox 分段有时不准，但聊胜于无）
            removed_any = False
            new_groups: dict[tuple[FigureType, str], list[CaptionStart]] = {}
            for fig_id, group in grouped_by_id.items():
                filtered = [c for c in group if c.paragraph_start]
                if filtered:
                    if len(filtered) < len(group):
                        removed_any = True
                    new_groups[fig_id] = filtered
                else:
                    new_groups[fig_id] = group
            grouped_by_id = new_groups

    result: list[CaptionStart] = []
    for group in grouped_by_id.values():
        per_page: dict[int, int] = {}
        for c in group:
            per_page[c.page] = per_page.get(c.page, 0) + 1
        if (
            len(group) > MaxDuplicateCaptionNames
            or max(per_page.values()) > MaxSamePageDuplicateCaptionNames
        ):
            continue  # 无法消歧，放弃该 (图型, 图号)
        result.extend(group)
    return result


def find_captions(pages: list[Page], layout) -> list[CaptionStart]:
    """找出文档里所有图注起始行（误报经 filter sieve 消歧）。

    Args:
        pages: 各页文本（text_extractor 产物）。
        layout: 文档级布局统计（DocumentLayout），字体过滤用其 font_counts；
            None（布局信息不足）时直接放弃图注识别，返回空列表——字体过滤依赖
            font_counts，无布局即无从消歧。

    Returns:
        消歧后的图注起始候选列表；layout 为 None 时返回空列表。

    构造过滤器列表顺序（照 Scala 实现）：
        1. Colon Only
        2. All Caps Figures Only
        3. All Caps Table Only
        4. Non Standard Font (Figure & Table) —— 若标准字体占比 > MinCommonFontPercentage
        5. Non Standard Font (Table)
        6. Non Standard Font (Figure)
        7. Abbreviated Fig Only
        8. Figure Following Text
        9. Period Only
        10. Left Aligned (all)
        11. Left Aligned Figures
        12. Line End Only
    """
    if layout is None:
        return []  # 布局信息不足（文本几乎抽不出），无字体信息可做消歧 → 放弃
    candidates = find_caption_candidates(pages)

    # 常见字体过滤：仅当文档确有占绝对多数的「标准字体」时才启用——
    # 否则字体信息不可信，启用反而会误删。font_counts 是原生字符计数，
    # 须先归一化成占比再与阈值比（MinCommonFontPercentage=0.4）
    font_filters: list[tuple[str, Callable[[CaptionStart], bool]]] = []
    if layout.font_counts:
        standard_font, count = max(layout.font_counts.items(), key=lambda kv: kv[1])
        if count / sum(layout.font_counts.values()) > MinCommonFontPercentage:
            font_filters = [
                (
                    "Non Standard Font: Set(Figure, Table)",
                    _non_standard_font(
                        standard_font, {FigureType.Figure, FigureType.Table}
                    ),
                ),
                (
                    "Non Standard Font: Set(Table)",
                    _non_standard_font(standard_font, {FigureType.Table}),
                ),
                (
                    "Non Standard Font: Set(Figure)",
                    _non_standard_font(standard_font, {FigureType.Figure}),
                ),
            ]

    filters = (
        [("Colon Only", _colon_only),
         ("All Caps Figures Only", _all_caps_fig_only),
         ("All Caps Table Only", _all_caps_table_only)]
        + font_filters
        + [("Abbreviated Fig Only", _abbreviated_fig_only),
            ("Figure Following Text", _figure_has_following_text_only),
            ("Period Only", _period_only),
            ("Left Aligned", _left_aligned_only(False)),
            ("Left Aligned Figures", _left_aligned_only(True)),
            ("Line End Only", _line_end_only)]
    )
    return select_caption_candidates(candidates, filters)


# ---------------------------------------------------------------------------
# CaptionBuilder：从起始行向后扩展成完整图注段落
# ---------------------------------------------------------------------------

# 图注扩展的启发式常量（值照 CaptionBuilder.scala）：
_ALIGNMENT_TOLERANCE = 2.0           # 左对齐/居中的像素容差
_GRAPHIC_INTERSECT_TOLERANCE = -2.0  # 与图形区相交的判定容差（负值=必须真正叠上）
_LARGE_PARAGRAPH_NUMBER_OF_LINES = 5.0
_MEDIAN_SPACING_PADDING = 0.2        # 行距上限 = 中位行距 + 该余量
_MAX_ADDITIONAL_SPACING = 2.0
# 右侧续行的判定：与上一行 y 差小、且 x 左缘贴近上一行右缘（行是往右延续而非另起一行）
_LINE_CONTINUATION_MAX_Y_DIFFERENCE = 3.0
_LINE_CONTINUATION_MAX_X_DIFFERENCE = 12.0
# 行距下限要留很大负数余量——PDFBox 对行高常严重高估，导致相邻行算成负间距
_MIN_Y_DIST_BETWEEN_LINES = -40
_LEFT_EDGE_DIFFERENCE_TOLERANCE = 30


def _get_line_font(line: Line) -> str | None:
    """整行是否同一种字体：全行各字符 font_name 都相同才返回该字体，否则 None。

    用于检测图注后续行是否换了字体（换字体通常意味着图注到此结束）。

    Args:
        line: Line，待检查的行

    Returns:
        全行统一字体名；字体不一致或空行返回 None。
    """
    fonts = [pos.font_name for w in line.words for pos in w.positions]
    if not fonts:
        return None
    return fonts[0] if all(f == fonts[0] for f in fonts) else None


@dataclass
class _CaptionBuilder:
    """扩展中的图注：已并入的行 + 边界 + 字体 + 是否仍保持居中。

    Attributes:
        lines: list[Line]，已并入图注的行
        boundary: Box，当前图注外接矩形
        font: str | None，图注整体字体（所有行同一字体时）
        centered: bool，是否仍保持居中
    """

    lines: list[Line]
    boundary: Box
    font: str | None          # 图注整体使用的字体（若所有行同一字体）
    centered: bool            # 是否仍保持居中（用于判断右对齐/居中是否被破坏）

    @property
    def last_line_right_aligned(self) -> bool:
        """最后一行是否与整个图注的右边界对齐（即不缩进）。"""
        return abs(self.boundary.x2 - self.lines[-1].boundary.x2) < 2.0

    def add_line(self, line: Line, new_boundary: Box, line_font: str | None) -> "_CaptionBuilder":
        """添加一行并更新状态。

        Args:
            line: Line，新并入的行
            new_boundary: Box，并入后新的整体边界
            line_font: str | None，该行的统一字体（不一致为 None）

        Returns:
            并入该行后的新 _CaptionBuilder（不修改原实例）。
        """
        # 新旧字体一致才延续「图注字体」标记，否则置 None（后续行换字体可据此停手）
        if self.font is not None and line_font is not None and self.font == line_font:
            new_font = line_font
        else:
            new_font = None
        still_centered = (
            self.centered and abs(line.boundary.xCenter - new_boundary.xCenter) < 2.0
        )
        return _CaptionBuilder(
            self.lines + [line], new_boundary, new_font, still_centered
        )


def _prune_caption_paragraph(paragraph: Paragraph) -> Paragraph:
    """把图注段落的高度裁到首词顶缘。

    PDFBox 对非常规字符常把行高估得离谱，而图注首词是 ASCII 文本、高度可靠，
    故用首词顶缘重定段落上界（照 CaptionBuilder.scala 的 pruneCaptionParagraph）。

    Args:
        paragraph: Paragraph，待裁剪的图注段落

    Returns:
        把上界重定为首词顶缘后的新段落。
    """
    pruned = paragraph.boundary.copy(y1=paragraph.lines[0].words[0].boundary.y1)
    return Paragraph(paragraph.lines, pruned)


def _build_caption(
    candidate: CaptionStart,
    caption_start_ids: set[int],
    lines_with_paragraphs: list[tuple[Line, Paragraph]],
    graphics_locations: list[Box],
    safe_line_spacing: float,
) -> CaptionParagraph:
    """把单个 CaptionStart 扩展成 CaptionParagraph。

    Args:
        candidate: 图注起始候选。
        caption_start_ids: 所有图注起始行的对象 id 集合（用于避免跨过另一个图注）。
        lines_with_paragraphs: (行, 所属段落) 列表，按顺序。
        graphics_locations: 图形区边界列表，用于避让。
        safe_line_spacing: 正常行距上限（= 中位行距 + padding）。

    Returns:
        扩展后的 CaptionParagraph。

    算法（逐行判断是否并入）：
        从起始行的下一行开始遍历，对每行评估：
        1. 若 y 间距 < _MIN_Y_DIST_BETWEEN_LINES 或 > safe_line_spacing + _MAX_ADDITIONAL_SPACING → 不并入。
        2. 若该行与图形区相交（除与首行重叠的图形外）→ 不并入。
        3. 若该行是另一个图注的起始行 → 不并入。
        4. 若该行是上一行的右侧续行（y 差小且 x 左缘靠近上一行右缘）→ 并入。
        5. 若换了字体且不是首行后立即（允许首行后换字体）→ 不并入。
        6. 若间距正常且左对齐 → 并入。
        7. 兜底：若水平重叠、不是大段开头、且不破坏右对齐/居中 → 并入。
        8. 否则停止。
    """
    start_idx = None
    for i, (line, _) in enumerate(lines_with_paragraphs):
        if line is candidate.line:
            start_idx = i
            break
    if start_idx is None:
        raise ValueError("No lines at candidate's location")

    starting_line = lines_with_paragraphs[start_idx][0]
    current = _CaptionBuilder(
        [starting_line], starting_line.boundary, _get_line_font(starting_line), True
    )

    # 与图注首行重叠的图形区视为「图注在图形内部」（如带边框的图），不去避让；
    # 其余图形区后续行若与之相交则停止扩展
    graphics_to_avoid = [
        bb
        for bb in graphics_locations
        if not bb.intersects(current.boundary, _GRAPHIC_INTERSECT_TOLERANCE)
    ]

    for line, paragraph in lines_with_paragraphs[start_idx + 1:]:
        line_bb = line.boundary
        current_boundary = current.boundary
        proposed_bb = current_boundary.container(line_bb)
        y_dist = line_bb.y1 - current_boundary.y2
        line_font = _get_line_font(line)
        first_line = len(current.lines) == 1
        # 起始行本身已到行尾（图号词占满行尾）时，下一行可以换字体（换个注释字体很正常）
        first_line_after_single_line_header = first_line and candidate.line_end
        font_change = (
            line_font is not None
            and current.font is not None
            and not first_line_after_single_line_header
            and line_font != current.font
        )

        if (
            y_dist < _MIN_Y_DIST_BETWEEN_LINES
            or y_dist > safe_line_spacing + _MAX_ADDITIONAL_SPACING
        ):
            # 与上一行间距太远/太近，不是同段内容
            use_line = False
        elif any(
            bb.intersects(proposed_bb, _GRAPHIC_INTERSECT_TOLERANCE)
            for bb in graphics_to_avoid
        ) or id(line) in caption_start_ids:
            # 撞上图形区，或这行是另一个图注的起始 → 不并入
            use_line = False
        elif (
            y_dist < _LINE_CONTINUATION_MAX_Y_DIFFERENCE
            and current.lines[-1].boundary.x2 - line_bb.x1
            < _LINE_CONTINUATION_MAX_X_DIFFERENCE
        ):
            # 是上一行右侧的续行（同段换行排到右边），并入
            use_line = True
        elif font_change and not first_line_after_single_line_header:
            # 换了字体，图注多半结束了
            use_line = False
        elif (
            y_dist < safe_line_spacing
            and y_dist > 0
            and abs(current_boundary.x1 - line_bb.x1) < _ALIGNMENT_TOLERANCE
        ):
            # 在正常行距内且与图注左对齐，视为图注续行
            use_line = True
        else:
            # 兜底：只要水平方向与图注重叠、不是新开的大段、也不破坏右对齐/居中，
            # 就并入——宁可多收，避免把靠得太近的图注续行漏掉
            centered = (
                abs(line_bb.xCenter - current_boundary.xCenter) < _ALIGNMENT_TOLERANCE
            )
            overlaps_horizontal = (
                line_bb.x1 < current_boundary.x2
                and line_bb.x2 > current_boundary.x1
                and (
                    current_boundary.x1 - proposed_bb.x1
                    < _LEFT_EDGE_DIFFERENCE_TOLERANCE
                    or first_line_after_single_line_header
                )
            )
            starting_large_paragraph = (
                line is paragraph.lines[0]
                and len(paragraph.lines) >= _LARGE_PARAGRAPH_NUMBER_OF_LINES
            )
            breaks_justification = not current.last_line_right_aligned and not (
                current.centered and centered
            )
            use_line = (
                overlaps_horizontal
                and not starting_large_paragraph
                and not breaks_justification
            )

        if use_line:
            current = current.add_line(line, proposed_bb, line_font)
        else:
            break

    caption_paragraph = _prune_caption_paragraph(
        Paragraph(current.lines, current.boundary)
    )
    return CaptionParagraph(
        candidate.name, candidate.fig_type, candidate.page, caption_paragraph
    )


def build_captions(
    starts: list[CaptionStart],
    graphics: list[Box],
    page: Page,
    median_line_spacing: float,
) -> list[CaptionParagraph]:
    """把一页内的图注起始扩展成完整图注段落。

    Args:
        starts: 该页的 CaptionStart 列表（来自 find_captions）。
        graphics: 该页的图形区包围盒列表（GraphicsExtractor 产物）。
        page: 该页 Page——须与 find_captions 传入的是同一批对象，行对象身份才能对齐。
        median_line_spacing: 文档中位行距（DocumentLayout.median_line_spacing）。

    Returns:
        扩展后的图注段落列表（顺序与 starts 一致）。

    说明：
        每个 CaptionStart 扩展时，需要知道所有图注起始行的 id，以避免吞并其他图注。
        同时传入页面对象以获取所有行列表。
    """
    if not starts:
        return []
    caption_start_ids = {id(s.line) for s in starts}
    lines_with_paragraphs = [
        (line, para) for para in page.paragraphs for line in para.lines
    ]
    return [
        _build_caption(
            c,
            caption_start_ids,
            lines_with_paragraphs,
            graphics,
            median_line_spacing + _MEDIAN_SPACING_PADDING,
        )
        for c in starts
    ]


def strip_caption_lines(page: Page, captions: list[CaptionParagraph]) -> None:
    """把图注行从所在段落移除（原地改 page.paragraphs），照 Paragraph.removeSpans。

    图注行若留在页面段落里，分类阶段会把这些小字行误判成图内文本（other_text，
    进 possibleFigureContent 喂给 _box_cuts_figure / crosses_center），图注跨图边框时
    还会抑制边框检测——所以图注一旦扩展成 CaptionParagraph，就从正文段落剥离。

    靠对象身份定位：build_captions 复用 find_captions 传入的同一批 Line 对象，
    图注段落里的行与 page.paragraphs 里的是同一批实例，用 id 精确匹配。段落里
    行被删光的整段移除；删掉部分行的按剩余行重算段落边界（不再含图注区域）。

    Args:
        page: 该页 Page（page.paragraphs 原地修改）。
        captions: 该页图注段落（行对象来自 page.paragraphs）。
    """
    if not captions:
        return
    caption_line_ids = {
        id(line) for c in captions for line in c.paragraph.lines
    }
    stripped: list[Paragraph] = []
    for paragraph in page.paragraphs:
        remaining = [
            line for line in paragraph.lines if id(line) not in caption_line_ids
        ]
        if not remaining:
            continue  # 行全被图注吃掉 → 整段移除
        if len(remaining) == len(paragraph.lines):
            stripped.append(paragraph)  # 没删到行，原样保留
        else:
            # 删掉部分行后按剩余行重算边界，避免段落外接矩形仍盖住图注区域
            stripped.append(
                Paragraph(remaining, Box_container([l.boundary for l in remaining]))
            )
    page.paragraphs[:] = stripped