"""文本抽取:rawdict → word/line/paragraph + 去页眉/页脚/页码。

pdffigures2 的 TextExtractor.scala + FormattingTextExtractor.scala 移植。
原始实现基于 PDFBox 的 PDFTextStripper 回调(逐字符 TextPosition),这里改用
PyMuPDF 的 page.get_text("rawdict")(char 级 bbox + span 的 size/font),分组语义
保持一致:

- char → word:span 内按空白字符切词,词 bbox = 各 char bbox 的外接矩形,
  positions 记录所在 span 的 size/font(每个字符一条,对应 Scala 的逐 char TextPosition)。
- word → line:span 逐行归并;is_horizontal 由整行首末非空白字符的 bbox 关系判定。
- line → paragraph:按行间隙/缩进聚合(TextExtractor 里 PDFTextStripper 的段落分组)。
- strip_formatting:跨页重复出现的顶部页眉 + 底缘页码剔除。

坐标直接复用 geometry 的 Box/Word/Line/Paragraph,不重复造几何。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

import fitz

from paperflow.vision.geometry import (
    Box,
    Box_container,
    Line,
    Paragraph,
    Position,
    Word,
)

# 顶部边距(72pt=1 英寸,取 3 英寸):此区域内的段落才是页眉候选(照 FormattingTextExtractor)
_TOP_MARGIN = 72 * 3
# 页码形态:「1」「23」这类十进制数字;不含罗马数字/Page 字样(Scala 亦如此)
_PAGE_NUMBER_RE = re.compile(r"^[1-9][0-9]*$")
# 两行间距超过 1.5 倍行高即视为新段落(PDFTextStripper 的段落间隙语义)
_PARA_GAP_RATIO = 1.5
# 新行相对前一行右缩进超过 0.3 倍行高 → 段落首行缩进,视为新段
_PARA_INDENT_RATIO = 0.3


@dataclass(frozen=True)
class Page:
    """一页文本:页码 + 按阅读顺序排列的段落列表。"""

    page_number: int
    paragraphs: list[Paragraph]


def extract_text(path_or_doc) -> list[Page]:
    """抽取 PDF 文本为按页划分的段落结构(rawdict → word/line/paragraph)。

    Args:
        path_or_doc: 文件路径(str/Path)或已打开的 fitz.Document。
            传路径时本函数负责打开与关闭;传 Document 则由调用方管理其生命周期。

    Returns:
        每页一个 Page,页码从 0 开始(与 fitz 的 page_number 一致)。
    """
    owns_doc = not isinstance(path_or_doc, fitz.Document)
    doc: fitz.Document = path_or_doc if not owns_doc else fitz.open(path_or_doc)
    try:
        pages: list[Page] = []
        for page_number, page in enumerate(doc):
            raw = page.get_text("rawdict")
            lines = _collect_lines(raw)
            paragraphs = _group_paragraphs(lines) if lines else []
            pages.append(Page(page_number, paragraphs))
        return pages
    finally:
        if owns_doc:
            doc.close()


def strip_formatting(pages: list[Page]) -> list[Page]:
    """剔除页眉/页脚/页码,返回只含正文段落的 Page 列表(照 FormattingTextExtractor)。

    判定依据都是「跨页一致」:页眉 = 顶部边距内、文本或高度跨页重复的段落;
    页码 = 底缘、纯数字、跨页重复出现的行。命中门槛是至少 minConsistent 页同时出现,
    避免把单页偶然出现的顶部/底部文本误当排版噪音删掉。

    Args:
        pages: 原始抽取出的 Page 列表。

    Returns:
        新的 Page 列表，移除了页眉/页脚/页码行，保留的段落/行对象身份不变。

    算法:
        1. 计算一致性门槛 min_consistent（随总页数变化）。
        2. 调用 _find_headers 找出每页的页眉段落。
        3. 调用 _find_page_number 找出每页的页码行。
        4. 遍历每页，剔除页眉段落；若段落包含页码行则移除该行，若行全部移除则整段丢弃。
    """
    min_consistent = _min_consistent_pages(len(pages))
    headers = _find_headers(pages, min_consistent)
    page_numbers = _find_page_number(pages, min_consistent)

    stripped: list[Page] = []
    for page, page_headers, page_number in zip(pages, headers, page_numbers):
        header_ids = {id(p) for p in page_headers}
        kept: list[Paragraph] = []
        for para in page.paragraphs:
            if id(para) in header_ids:
                continue  # 页眉整段剔除
            if page_number is not None and any(ln is page_number for ln in para.lines):
                lines = [ln for ln in para.lines if ln is not page_number]
                if not lines:
                    continue
                para = _make_paragraph(lines)  # 剔除页码行后重打包段落
            kept.append(para)
        stripped.append(Page(page.page_number, kept))
    return stripped


# ---------------------------------------------------------------------------
# rawdict → word/line
# ---------------------------------------------------------------------------


def _collect_lines(raw: dict) -> list[Line]:
    """把一页的 rawdict 拍平成按阅读顺序排列的 Line 列表;非文本块跳过。

    Args:
        raw: PyMuPDF 的 page.get_text("rawdict") 返回的字典。

    Returns:
        该页所有文本行的 Line 对象列表（按 rawdict 中的顺序）。
    """
    lines: list[Line] = []
    for block in raw["blocks"]:
        if block.get("type", 0) != 0:
            continue  # 图片/图形块没有可抽取文本
        for raw_line in block["lines"]:
            line = _line_to_line(raw_line)
            if line is not None:
                lines.append(line)
    return lines


def _line_to_line(raw_line: dict) -> Optional[Line]:
    """把 rawdict 的一条 line 转成 geometry 的 Line。

    Args:
        raw_line: rawdict 中的一个 "lines" 项。

    Returns:
        转换后的 Line 对象，若无有效词则返回 None。

    算法:
        1. 遍历 span，按其 chars 切分单词（遇到空白字符切分，词不跨 span）。
        2. 每个字符生成一个 Position 记录字体和字号。
        3. 用首末非空白字符的 y 区间是否重叠判断行是否水平。
    """
    words: list[Word] = []
    first_bbox: Optional[tuple] = None  # 整行首/末非空白字符 bbox,供 is_horizontal 判定
    last_bbox: Optional[tuple] = None

    for span in raw_line["spans"]:
        size = span["size"]
        font = span["font"]
        current: list[tuple[tuple, str]] = []  # 当前词累积的 (bbox, 字符)
        for ch in span["chars"]:
            bbox = ch["bbox"]
            c = ch["c"]
            if c.isspace():
                word = _make_word(current, size, font)
                if word is not None:
                    words.append(word)
                current = []
            else:
                if first_bbox is None:
                    first_bbox = bbox
                last_bbox = bbox
                current.append((bbox, c))
        # span 末尾收尾当前词,词不跨 span
        word = _make_word(current, size, font)
        if word is not None:
            words.append(word)

    if not words:
        return None
    boundary = Box_container([w.boundary for w in words])
    return Line(words, boundary, _is_horizontal(first_bbox, last_bbox))


def _make_word(chars: list[tuple[tuple, str]], size: float, font: str) -> Optional[Word]:
    """把一组 (bbox, 字符) 打包成一个 Word:bbox 取 container,positions 逐字符记录。

    Args:
        chars: (bbox, 字符) 元组的列表，其中 bbox 是 (x0,y0,x1,y1) 元组。
        size: 该 span 的字号（所有字符共享）。
        font: 该 span 的字体名。

    Returns:
        生成的 Word 对象，若 chars 为空则返回 None。
    """
    if not chars:
        return None
    boundary = Box_container([Box(*bbox) for bbox, _ in chars])
    text = "".join(c for _, c in chars)
    return Word(text, boundary, [Position(size, font) for _ in chars])


def _is_horizontal(first_bbox: Optional[tuple], last_bbox: Optional[tuple]) -> bool:
    """水平行 = 首末字符的 y 区间重叠(位于同一条基线附近);竖直/旋转文本则无重叠。

    Args:
        first_bbox: 整行第一个非空白字符的 bbox 元组。
        last_bbox: 整行最后一个非空白字符的 bbox 元组。

    Returns:
        True 如果水平，否则 False。若任一为 None 则默认 True。
    """
    if first_bbox is None or last_bbox is None:
        return True
    _, fy1, _, fy2 = first_bbox
    _, ly1, _, ly2 = last_bbox
    return not (fy2 < ly1 or ly2 < fy1)


# ---------------------------------------------------------------------------
# line → paragraph
# ---------------------------------------------------------------------------


def _group_paragraphs(lines: list[Line]) -> list[Paragraph]:
    """按行间隙/缩进把 Line 聚合为 Paragraph(对应 TextExtractor 的段落分组)。

    Args:
        lines: 已排序的行列表。

    Returns:
        段落列表。

    算法:
        从第一行开始累积。对每一新行：
        - 若 gap > 1.5 * max(prev.height, line.height) → 新段落
        - 若 line 的左缘比 prev 的左缘大超过 0.3 * height（首行缩进）→ 新段落
        - 否则并入当前段落
    """
    paragraphs: list[Paragraph] = []
    current: list[Line] = [lines[0]]
    for line in lines[1:]:
        prev = current[-1]
        gap = line.boundary.y1 - prev.boundary.y2
        height = max(prev.boundary.height, line.boundary.height)
        indented = line.boundary.x1 > prev.boundary.x1 + _PARA_INDENT_RATIO * height
        if gap > _PARA_GAP_RATIO * height or indented:
            paragraphs.append(_make_paragraph(current))
            current = [line]
        else:
            current.append(line)
    paragraphs.append(_make_paragraph(current))
    return paragraphs


def _make_paragraph(lines: list[Line]) -> Paragraph:
    """用行列表构造 Paragraph，边界取各行的外接矩形。

    Args:
        lines: 行列表（非空）。

    Returns:
        Paragraph 对象。
    """
    return Paragraph(lines, Box_container([ln.boundary for ln in lines]))


# ---------------------------------------------------------------------------
# strip_formatting 内部:页眉/页码判定
# ---------------------------------------------------------------------------


def _min_consistent_pages(n_pages: int) -> int:
    """一致性门槛:页数越少要求越严格(照 FormattingTextExtractor)。

    页数越少越难凑齐跨页重复,故门槛随页数减少而收紧,防止误删。

    Args:
        n_pages: 总页数。

    Returns:
        需要出现一致现象的最少页数。
    """
    if n_pages < 3:
        return n_pages - 0
    if n_pages < 5:
        return n_pages - 1
    return n_pages - 2


def _find_headers(pages: list[Page], min_consistent: int) -> list[list[Paragraph]]:
    """找每页页眉(照 findHeaders):每页取顶部两个候选,再跨页一致性筛选。

    Args:
        pages: 所有页。
        min_consistent: 一致性门槛。

    Returns:
        每页的页眉段落列表（每页可能 0、1 或 2 个）。

    算法:
        对每页取顶部区域内、行数<=3、且不与相邻段落重叠的两个独立段落作为候选。
        先对第一候选进行跨页文本/高度一致性筛选，再对第二候选（仅在第一候选被采纳的页上）筛选。
    """
    first_candidates: list[Optional[Paragraph]] = []
    second_candidates: list[Optional[Paragraph]] = []
    for page in pages:
        top = [p for p in page.paragraphs if p.boundary.y1 < _TOP_MARGIN]
        top2 = sorted(top, key=lambda p: p.boundary.y1)[:2]
        first: Optional[Paragraph] = None
        second: Optional[Paragraph] = None
        if top2:
            candidate = top2[0]
            if len(candidate.lines) <= 3 and _is_above_other_text(candidate, top):
                first = candidate
                # 第二候选:同一页顶部的第二个独立段落(常是两行页眉的第二行)
                if len(top2) > 1:
                    candidate2 = top2[1]
                    if (
                        len(candidate2.lines) <= 3
                        and _is_above_other_text(candidate2, top, first)
                    ):
                        second = candidate2
        first_candidates.append(first)
        second_candidates.append(second)

    first_headers = _select_header_candidates(pages, first_candidates, min_consistent)
    # 第二候选只在第一候选已被采纳的页才有意义(否则单独出现说明不是页眉)
    pruned_second = [
        sc if (sc is not None and fh is not None) else None
        for sc, fh in zip(second_candidates, first_headers)
    ]
    second_headers = _select_header_candidates(pages, pruned_second, min_consistent)
    return [
        [p for p in (first, second) if p is not None]
        for first, second in zip(first_headers, second_headers)
    ]


def _select_header_candidates(
    pages: list[Page],
    candidates: list[Optional[Paragraph]],
    min_consistent: int,
) -> list[Optional[Paragraph]]:
    """从各页候选页眉里挑出跨页一致的真实页眉(照 selectHeaderCandidates)。

    Args:
        pages: 所有页。
        candidates: 每页的一个候选段落（或 None）。
        min_consistent: 一致性门槛。

    Returns:
        每页筛选后的段落（或 None）。

    算法:
        1. 若有效候选数 < min_consistent，全部放弃。
        2. 优先按文本完全一致分组，若某组数量达门槛，则采纳该组。
        3. 否则按高度（y1,y2）一致（±1pt）分组，若某组数量达门槛则采纳该组。
        4. 否则全部放弃。
    """
    non_empty = [c for c in candidates if c is not None]
    if len(non_empty) < min_consistent:
        return [None] * len(pages)

    # 第一优先:文本完全一致
    counts: dict[str, int] = {}
    for c in non_empty:
        counts[c.text] = counts.get(c.text, 0) + 1
    most_common_text = max(counts, key=counts.get)
    if counts[most_common_text] >= min_consistent:
        return [
            c if c is not None and c.text == most_common_text else None
            for c in candidates
        ]

    # 退回:高度一致(±1pt 内视为同一位置)
    common: Optional[Paragraph] = None
    for c in non_empty:
        if (
            sum(
                1
                for d in non_empty
                if abs(d.boundary.y1 - c.boundary.y1) < 1.0
                and abs(d.boundary.y2 - c.boundary.y2) < 1.0
            )
            >= min_consistent
        ):
            common = c
            break
    if common is not None:
        return [
            c
            if c is not None
            and abs(c.boundary.y1 - common.boundary.y1) < 1.0
            and abs(c.boundary.y2 - common.boundary.y2) < 1.0
            else None
            for c in candidates
        ]
    return [None] * len(pages)


def _is_above_other_text(
    candidate: Paragraph,
    top_paragraphs: list[Paragraph],
    also_exclude: Optional[Paragraph] = None,
) -> bool:
    """候选是否「悬在其他文本之上」:与任一其他段落在垂直方向重叠(±3pt 内)即不合格。

    Args:
        candidate: 待检查的段落。
        top_paragraphs: 顶部区域的所有段落。
        also_exclude: 另一个需要排除的段落（通常是第一候选）。

    Returns:
        True 如果 candidate 不与其他任何段落（除自身和 also_exclude）在垂直方向重叠。
    """
    excluded = {id(candidate)}
    if also_exclude is not None:
        excluded.add(id(also_exclude))
    return all(
        id(p) in excluded or abs(candidate.boundary.y2 - p.boundary.y2) > 3
        for p in top_paragraphs
    )


def _find_page_number(
    pages: list[Page], min_consistent: int
) -> list[Optional[Line]]:
    """每页底缘的纯数字行页码(照 findPageNumber)。

    Args:
        pages: 所有页。
        min_consistent: 一致性门槛。

    Returns:
        每页的页码行（或 None）。

    算法:
        取每页最下方的段落的最后一行，若该行文本为纯数字（如 "12"）则作为候选。
        若候选数达到 min_consistent，则全部返回；否则全部放弃。
    """
    candidates: list[Optional[Line]] = []
    for page in pages:
        if not page.paragraphs:
            candidates.append(None)
            continue
        bottom = max(page.paragraphs, key=lambda p: p.boundary.y2)
        last_line = bottom.lines[-1]
        candidates.append(last_line if _PAGE_NUMBER_RE.match(last_line.text) else None)
    if sum(1 for c in candidates if c is not None) >= min_consistent:
        return candidates
    return [None] * len(pages)