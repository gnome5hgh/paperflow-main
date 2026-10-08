"""正文/图内文本分类：RegionClassifier（pdffigures2 的 RegionClassifier.scala 移植）。

把一页文本按「是否图内文本」分类成正文段（body_text）与图内段（other_text），供
FigureDetector 划定图边界、给 proposal 打分。

核心是两层：
- splitAroundCaptions：段落外接矩形与图注重叠时按行拆成子段——图注常被 PDFBox
  与相邻正文合并进同一段，不拆开会污染正文分类。拆分出的子段与 body_text/
  other_text 统一按「阅读序」累积（首段在前），有意偏离 Scala 的 :: 前插倒序。
- classifyRegions：一组启发式「筛子」按固定顺序逐个判定段落，首个命中者定案，
  默认归正文。筛子里的图内文本信号（图形重叠/竖排/宽间距/小字号）排前、正文信号
  （行宽/标题/边距）排后——前者是高置信「这是图内文本」的负信号，先识别出来，
  再对剩余段落用正信号认正文。

另含图形包围盒检测：图形完整包住一个图注、且没有段落跨出图形时，该图形是图的
过程边框而非图本体——边框并入 non_figure_graphics（正文区域），图本体裁 3pt 保留。

本文件不涉及 SectionTitleExtractor（本子项目跳过），IsTitle 用简化近似实现，
近似点见 _is_title_style 的 docstring。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass

from paperflow.vision.parsers.caption import CaptionParagraph
from paperflow.vision.parsers.document_layout import LINE_WIDTH_BUCKET_SIZE, DocumentLayout
from paperflow.vision.geometry import Box, Line, Paragraph
from paperflow.vision.parsers.text_extractor import Page

# 段落与图形区重叠面积 / 段落面积 超过该比例 → 图内文本（照 GraphicOverlaps）
_GRAPHIC_OVERLAP_RATIO = 0.20
# 平均词距超过正文平均词距该值(pt) 才算「宽间距」图内文本（照 Spacing）
_SPACING_WIDE_OFFSET = 5.0
# 超出标准字号该值(pt) 的字符算「非标准大字」；占比超 _FONT_RATIO 则整段是大字
_LARGE_FONT_OFFSET = 1.0
# 低于标准字号该值(pt) 的字符算「小字」；占比超 _FONT_RATIO 则整段是小字
_SMALL_FONT_OFFSET = 0.1
# 大字/小字字符占比门槛（照 Spacing/SmallFont 的 >0.95）
_FONT_RATIO = 0.95
# 段落与图注重叠的判定容差：负值要求真正重叠 2pt（边界接触不算，照 splitAroundCaptions）
_CAPTION_SPLIT_MARGIN = -2
# 图注面积 / 图形面积 < 该值才算「图注在图内」（否则图形就是图注本体）
_FIGURE_BBOX_CAPTION_RATIO = 0.50
# 图形包围盒内收量(pt)：避免返回的图形区裁到边框本身
_BORDER_CROP = 3
# Margins 兜底：不信任左边距时面积 > 该值才可能是正文段
_MARGIN_BODY_AREA = 7000
# Margins 兜底：信任左边距时还需面积 > 该值
_MARGIN_MIN_AREA = 100
# Margins 兜底：段落左缘落在左边距上的共享比例门槛
_MARGIN_ALIGNMENT = 0.18

# ---- IsTitle（简化近似）----
# 中轴校准除数：中轴线 = xCenter - standardWidth / 2（照 isAlignedOrCentered）
_TEXT_ALIGNMENT_TOLERANCE = 2.0
# 左缘/中轴落在左边距上的共享比例门槛（照 MinSharedMargin）
_MIN_SHARED_MARGIN = 0.1
# 标题首词编号前缀（照 SectionTitleExtractor 的 isPrefixed 正则）
_NUMBER_RE = re.compile(r"^[1-9][0-9]*(.[1-9][0-9]*)*.?$")
_ROMAN_NUMERALS_RE = re.compile(r"^[IVX]+.?$")
_LETTER_NUMBER_RE = re.compile(r"^[A-Z](.|[1-9]*.?)$")
_APPENDIX_RE = re.compile(r"^appendix.?$", re.IGNORECASE)


@dataclass(frozen=True)
class PageWithBodyText:
    """一页分类结果：正文/图内文本段落 + 图形包围盒检测后的图形与边框。

    Attributes:
        page_number: 页码。
        classified_text: 全页文本（由段落文本拼出，供下游展示）。
        captions: 该页图注（原样透传）。
        graphics: 图形区（检测为图过程边框的图形裁 3pt 后仍保留在此）。
        non_figure_graphics: 非图侧栏/色带 + 检测出的图边框（四条零宽/零高的线）。
        body_text: 正文段落。
        other_text: 图内文本段落。
    """

    page_number: int
    classified_text: str
    captions: list[CaptionParagraph]
    graphics: list[Box]
    non_figure_graphics: list[Box]
    body_text: list[Paragraph]
    other_text: list[Paragraph]


def classify_regions(
    page: Page,
    captions: list[CaptionParagraph],
    graphics: list[Box],
    non_figure_graphics: list[Box],
    layout: DocumentLayout,
) -> PageWithBodyText:
    """把一页文本分类为正文/图内文本，并检测图形包围盒（照 classifyRegions）。

    Args:
        page: 该页文本（含按阅读顺序排列的段落）。
        captions: 该页图注（CaptionParagraph 列表）。
        graphics: 该页图形区包围盒（extract_graphics 的 graphics）。
        non_figure_graphics: 该页非图侧栏/色带（nonFigureGraphics）。
        layout: 文档级布局统计（DocumentLayout）。

    Returns:
        分类后的页：body_text/other_text 分别为正文与图内文本段；
        graphics/non_figure_graphics 是图形包围盒检测后的结果。

    算法：
        1. 用 _split_around_captions 拆分与图注重叠的段落，避免图注污染正文分类。
        2. 用一组顺序固定的分类器逐个判定每个段落，首个返回非 None 者定案：
           图内文本信号（图形重叠/竖排/宽间距/小字号）先识别，正文信号（行宽/标题/边距）后识别；
           若所有分类器都返回 None，则默认归为正文。
        3. 图形包围盒检测：若某图形完整包含图注、且无任何段落跨越图形边界，
           则该图形是图的过程边框（如方框边框），将其四边作为 non_figure_graphics，
           图形内部裁 3pt 后保留在 graphics 中。
    """
    # 段落若与图注重叠，先按行拆开（图注与正文常被合并进同一段）
    separated = [
        split
        for paragraph in page.paragraphs
        for split in _split_around_captions(paragraph, captions)
    ]

    # 分类筛：顺序重要，首个命中者定案（照 RegionClassifier.scala）。
    # 图内文本信号排前（高置信负信号，一旦命中即 other_text），正文信号排后。
    classifiers = [
        lambda p: _graphic_overlaps(p, graphics),
        _vertical_text,
        lambda p: _spacing(p, layout.standard_font_size, layout.average_word_spacing),
        lambda p: _line_width(p, layout.standard_width_bucketed),
        lambda p: _small_font(p, layout.standard_font_size),
        lambda p: _is_title(p, layout),
        lambda p: _margins(p, layout.trust_left_margin, layout.left_margins),
    ]

    body_text = []
    other_text = []
    for paragraph in separated:
        classification = None
        for classifier in classifiers:
            result = classifier(paragraph)
            if result is not None:
                classification = result
                break
        is_body = True if classification is None else classification
        (body_text if is_body else other_text).append(paragraph)

    # 图形包围盒检测：图形完整包住图注、且没有段落跨出图形 → 该图形是图的过程边框。
    # 边框并入 non_figure_graphics（正文区域，避免图边界延伸到边框外），
    # 图本体裁 3pt 保留在 graphics（避免返回区域裁到边框）。
    figures_bounding_box_graphics = []
    rest_graphics = []
    for graphic in graphics:
        # 条件1：图形包含至少一个图注，且图注面积占图形面积 < 50%（否则图形本身就是图注）
        contains_caption = any(
            graphic.contains(c.boundary)
            and graphic.intersectArea(c.boundary) / graphic.area < _FIGURE_BBOX_CAPTION_RATIO
            for c in captions
        )
        # 条件2：没有段落跨越图形边界（即段落要么完全在图形内，要么完全在图形外）
        has_straddling_paragraph = any(
            not graphic.contains(p.boundary) and p.boundary.intersects(graphic)
            for p in page.paragraphs
        )
        if contains_caption and not has_straddling_paragraph:
            figures_bounding_box_graphics.append(graphic)
        else:
            rest_graphics.append(graphic)

    # 将包围盒拆成四条零宽/零高的边框线（左边框、右边框、上边框、下边框）
    figure_bounding_boxes = [
        border
        for box in figures_bounding_box_graphics
        for border in (
            box.copy(x2=box.x1),   # 左边框（零宽）
            box.copy(x1=box.x2),   # 右边框（零宽）
            box.copy(y2=box.y1),   # 上边框（零高）
            box.copy(y1=box.y2),   # 下边框（零高）
        )
    ]
    # 图形本体向内收缩 3pt，避免裁到边框本身
    cropped_figure_graphics = [
        box.copy(
            x1=box.x1 + _BORDER_CROP,
            x2=box.x2 - _BORDER_CROP,
            y1=box.y1 + _BORDER_CROP,
            y2=box.y2 - _BORDER_CROP,
        )
        for box in figures_bounding_box_graphics
    ]

    return PageWithBodyText(
        page_number=page.page_number,
        classified_text=" ".join(p.text for p in page.paragraphs),
        captions=captions,
        graphics=rest_graphics + cropped_figure_graphics,
        non_figure_graphics=non_figure_graphics + figure_bounding_boxes,
        body_text=body_text,
        other_text=other_text,
    )


# ---------------------------------------------------------------------------
# splitAroundCaptions：段落与图注重叠时按行拆分
# ---------------------------------------------------------------------------


def _split_around_captions(
    paragraph: Paragraph, captions: list[CaptionParagraph]
) -> list[Paragraph]:
    """段落与图注重叠时按行拆成不再叠图注的子段；无法拆时原样返回。

    图注常被 PDFBox 与相邻正文合并成同一段，不拆开的话正文段会被图注污染。

    Args:
        paragraph: 待拆分的段落。
        captions: 该页的图注列表。

    Returns:
        拆分后的段落列表（若无需拆分则返回 [paragraph]）。

    算法：
        1. 检查段落的整体边界是否与任一图注重叠（容差 _CAPTION_SPLIT_MARGIN=-2，要求真正重叠）。
        2. 若所有行都与图注重叠，说明整段就是图注本身，无法拆分，原样返回。
        3. 否则逐行累积：每当累积行的外接矩形碰上图注，就在此处切段。
        4. 返回的子段按阅读序排列（首段在前）——有意偏离 Scala 的 :: 前插倒序。
    """
    caption_boundaries = [
        c.boundary
        for c in captions
        if paragraph.boundary.intersects(c.boundary, _CAPTION_SPLIT_MARGIN)
    ]
    if not caption_boundaries:
        return [paragraph]
    if all(_intersects_any(line.boundary, caption_boundaries) for line in paragraph.lines):
        return [paragraph]
    # 逐行累积；一旦累积行的外接矩形碰到图注就切出新段。
    # 段间顺序用正序累积（append），返回的子段为阅读序（首段在前）。有意偏离
    # Scala 原实现的 :: 前插倒序：下游 Figure.imageText 拼接词文本时与直觉一致，
    # 分类逐段独立判定，段序本身不影响分类结果。
    split_paragraphs: list[Paragraph] = []
    new_lines = [paragraph.lines[0]]
    new_box = paragraph.lines[0].boundary
    for next_line in paragraph.lines[1:]:
        combined_box = next_line.boundary.container(new_box)
        if _intersects_any(combined_box, caption_boundaries):
            split_paragraphs.append(Paragraph(list(new_lines), new_box))
            new_lines = [next_line]
            new_box = next_line.boundary
        else:
            new_box = combined_box
            new_lines.append(next_line)
    split_paragraphs.append(Paragraph(list(new_lines), new_box))
    return split_paragraphs


def _intersects_any(box: Box, boxes: list[Box]) -> bool:
    """box 是否与列表中任一框相交（容差 0，对应 Box.scala 的 intersectsAny）。

    Args:
        box: Box，待检查的矩形
        boxes: list[Box]，候选矩形集合

    Returns:
        True 表示 box 与 boxes 中任一矩形相交。
    """
    return any(box.intersects(b) for b in boxes)


# ---------------------------------------------------------------------------
# 分类筛子：每个启发式返回 Optional[bool]
#   True  = 断言是正文；False = 断言是图内文本；None = 无意见，交给下一个。
# ---------------------------------------------------------------------------


def _graphic_overlaps(paragraph: Paragraph, graphics: list[Box]) -> bool | None:
    """与图形区重叠面积 >20% 的段 → 图内文本（图内文字浮在图形上）。

    Args:
        paragraph: 待判定的段落。
        graphics: 图形区边界列表。

    Returns:
        False（图内文本）若重叠比例超阈值，否则 None。
    """
    b = paragraph.boundary
    if b.area == 0:
        return None  # 零面积段无从算重叠比例，交给后续启发式
    if any(g.intersectArea(b) / b.area > _GRAPHIC_OVERLAP_RATIO for g in graphics):
        return False
    return None


def _vertical_text(paragraph: Paragraph) -> bool | None:
    """全行竖直/旋转文本 → 图内文本（竖直轴标签、旋转标注等）。

    Args:
        paragraph: 待判定的段落。

    Returns:
        False（图内文本）若所有行都不是水平方向，否则 None。
    """
    if all(not line.is_horizontal for line in paragraph.lines):
        return False
    return None


def _spacing(
    paragraph: Paragraph,
    standard_font_size: float | None,
    average_word_spacing: float,
) -> bool | None:
    """宽词距（且非大字）→ 图内文本：图例/轴标常被排成稀疏词距。

    大字排除：全角大字段更可能是标题而非图内文本，先排除再谈宽间距。

    Args:
        paragraph: 待判定的段落。
        standard_font_size: 文档标准字号（可能为 None）。
        average_word_spacing: 文档平均词间距。

    Returns:
        False（图内文本）若平均词距 > 平均词距 + 5pt 且非大字，否则 None。
    """
    word_spaces = [
        pair[1].boundary.x1 - pair[0].boundary.x2
        for line in paragraph.lines
        for pair in zip(line.words, line.words[1:])
    ]
    positive = [s for s in word_spaces if s > 0]
    wide_spacing = bool(positive) and (
        sum(positive) / len(positive) > average_word_spacing + _SPACING_WIDE_OFFSET
    )
    large_font = False
    if standard_font_size is not None:
        total = 0
        non_standard = 0
        for line in paragraph.lines:
            for word in line.words:
                for pos in word.positions:
                    total += 1
                    if pos.font_size - standard_font_size > _LARGE_FONT_OFFSET:
                        non_standard += 1
        large_font = non_standard / total > _FONT_RATIO
    if not large_font and wide_spacing:
        return False
    return None


def _line_width(paragraph: Paragraph, standard_width_bucketed: float | None) -> bool | None:
    """多行且行宽≈标准行宽的段 → 正文（正文整段铺满栏宽，图内文本通常窄）。

    Args:
        paragraph: 待判定的段落。
        standard_width_bucketed: 文档标准行宽（2pt 桶，可能为 None）。

    Returns:
        True（正文）若行数 >2 且段落宽度接近标准行宽（误差 < LINE_WIDTH_BUCKET_SIZE），
        否则 None。
    """
    if (
        len(paragraph.lines) > 2
        and standard_width_bucketed is not None
        and abs(paragraph.boundary.width - standard_width_bucketed) < LINE_WIDTH_BUCKET_SIZE
    ):
        return True
    return None


def _small_font(paragraph: Paragraph, standard_font_size: float | None) -> bool | None:
    """>95% 字符低于标准字号 0.1pt → 图内小字（轴标、标注、图注正文）。

    Args:
        paragraph: 待判定的段落。
        standard_font_size: 文档标准字号（可能为 None）。

    Returns:
        False（图内文本）若小字号字符占比 > _FONT_RATIO，否则 None。
    """
    if standard_font_size is None:
        return None
    total = 0
    small = 0
    for line in paragraph.lines:
        for word in line.words:
            for pos in word.positions:
                total += 1
                if standard_font_size - pos.font_size > _SMALL_FONT_OFFSET:
                    small += 1
    if small / total > _FONT_RATIO:
        return False
    return None


def _is_title(paragraph: Paragraph, layout: DocumentLayout) -> bool | None:
    """标题（居中/左对齐 + 大写/编号起头 + 全行同字号）→ 正文。

    简化近似：本子项目不移植 SectionTitleExtractor，isTitleStyle 用
    「全行同一字体字号 + 全大写或非常见字体」近似（见 _is_title_style），
    对齐与起头判定照抄原 isAlignedOrCentered / isTitleStartText。

    Args:
        paragraph: 待判定的段落。
        layout: 文档布局统计。

    Returns:
        True（正文）若满足标题特征，否则 None。
    """
    if (
        _aligned_or_centered(paragraph.boundary, layout)
        and _is_title_start_text(paragraph.lines[0])
        and all(_is_title_style(line, layout) for line in paragraph.lines)
    ):
        return True
    return None


def _aligned_or_centered(region: Box, layout: DocumentLayout) -> bool:
    """区域是否与正文左对齐或居中（照 isAlignedOrCentered）。

    左对齐 = 区域左缘落在常见左边距；居中 = 区域中轴落到「标准行宽中点对准的
    左边距」（中轴线 = xCenter - standardWidth / 2）。左边距按左缘取整后两边
    查表再求和，与区域宽度无关。

    Args:
        region: 待判定的区域。
        layout: 文档布局统计。

    Returns:
        True 若区域左对齐或居中。
    """
    is_center = False
    if layout.standard_width_bucketed is not None:
        x1 = region.xCenter - layout.standard_width_bucketed / _TEXT_ALIGNMENT_TOLERANCE
        is_center = (
            layout.left_margins.get(math.ceil(x1), 0.0)
            + layout.left_margins.get(math.floor(x1), 0.0)
        ) > _MIN_SHARED_MARGIN
    left_aligned = False
    if layout.trust_left_margin:
        x1 = region.x1
        left_aligned = (
            layout.left_margins.get(math.ceil(x1), 0.0)
            + layout.left_margins.get(math.floor(x1), 0.0)
        ) > _MIN_SHARED_MARGIN
    return left_aligned or is_center


def _is_title_start_text(line: Line) -> bool:
    """首行是否像标题起头：非单字符且以大写字母或编号/小节前缀开头（照 isTitleStartText）。

    Args:
        line: 段落的首行。

    Returns:
        True 若该行看起来像标题的开头。
    """
    text = line.text
    return len(text) > 1 and (text[0].isupper() or _is_prefixed(line))


def _is_prefixed(line: Line) -> bool:
    """首词是否带编号前缀（如「1.」「3.2」「III」「(a)」或附录词），照 isPrefixed。

    Args:
        line: 待检查的行。

    Returns:
        True 若首词匹配编号前缀正则。
    """
    if len(line.words) == 1:
        return False
    first = line.words[0].text
    return bool(
        _NUMBER_RE.match(first)
        or _ROMAN_NUMERALS_RE.match(first)
        or _LETTER_NUMBER_RE.match(first)
        or _APPENDIX_RE.match(first)
    )


def _is_title_style(line: Line, layout: DocumentLayout) -> bool:
    """标题样式近似：全行同一字体同一字号，且全大写或使用了非常见字体。

    SectionTitleExtractor.isTitleStyle 的简化：原实现还核对非标准字符集与
    字号相对标准的差异，这里只保留「样式统一 + 与正文可区分」两个核心信号。
    全小写的正文行（同字体同字号）不会命中，避免把普通段落误判成标题。

    Args:
        line: 待检查的行。
        layout: 文档布局统计（提供 font_counts）。

    Returns:
        True 若该行样式符合标题特征（样式统一且全大写或使用罕见字体）。
    """
    styles = [(pos.font_name, pos.font_size) for w in line.words for pos in w.positions]
    if not styles:
        return False
    same_style = all(s == styles[0] for s in styles)
    total_chars = sum(layout.font_counts.values())
    rare_font = bool(total_chars) and any(
        layout.font_counts.get(name, 0) / total_chars < 0.1 for name, _ in styles
    )
    is_all_caps = not any(c.islower() for c in line.text)
    return same_style and (is_all_caps or rare_font)


def _margins(
    paragraph: Paragraph, trust_left_margin: bool, left_margins: dict[int, float]
) -> bool | None:
    """边距兜底：小段或不贴左边距 → 图内文本；否则正文。

    不信任左边距时只看面积（>7000pt² 才可能是正文段）；
    信任时需「左缘落在常见左边距上」且面积 >100pt²。

    Args:
        paragraph: 待判定的段落。
        trust_left_margin: 是否信任左边距统计。
        left_margins: 左边距字典（x1 取整 → 占比）。

    Returns:
        True（正文）若满足边距条件，否则 False（图内文本）。
    """
    b = paragraph.boundary
    if not trust_left_margin:
        return b.area > _MARGIN_BODY_AREA
    aligned = (
        left_margins.get(math.floor(b.x1), 0.0)
        + left_margins.get(math.ceil(b.x1), 0.0)
    ) > _MARGIN_ALIGNMENT
    return aligned and b.area > _MARGIN_MIN_AREA