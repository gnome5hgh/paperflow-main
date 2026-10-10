"""本地 PDF 文本抽取（PyMuPDF）：把一个 PDF 读成 markdown 文本，不依赖任何外部服务。

是本项目「读 PDF」的唯一实现，服务三个消费方：`read_pdf` 工具要 markdown 全文、
RAG 索引要带版面坐标的渲染块、引用语料索引只要一个标题（轻路径 `pdf_title`，
不抽全文）。三条路共用同一处标题判据，取到的标题必然一致。

输出形态由版面推断：PyMuPDF 只给出带字号与坐标的文本行，本模块按字号识别章节标题并
分级，其余行按原文换行合成段落。文本顺序**沿用 PyMuPDF 给出的块顺序**——那是版面的
阅读顺序，双栏论文靠它才是「先左栏到底、再右栏」；按坐标重排反而会把两栏交错。

标题口径是**宁空勿错**：判据只看「明确不是标题」的形态（占位词、文件名、页码戳、
页眉/期刊行、水印、署名行），过了判据也不代表一定是标题，所以拿不准就返回空串，
绝不用文件名或页眉顶替——空标题只是不索引这一篇，错标题会同时进每块前缀与引用匹配。

表格不单独识别：实测 ``page.find_tables()`` 在常规学术 PDF 上几乎检不出东西（表格以
文字与线条绘制），而表格里的数字本就躺在文本层，随普通文本一起读出来即可。
"""
from __future__ import annotations

import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

# 缓存上限：一篇论文的正文可达数百 KB，留几十篇的量级足以覆盖一次会话里
# 「同一篇被多个 agent 反复读」的重复，又不至于让进程内存无界增长。
_CACHE_MAX_ENTRIES = 32

# 标题行长度上限：字号偏大的长段落（如整段加粗的摘要）不该被当成章节标题。
_MAX_HEADING_CHARS = 100

# 判定标题的字号放大倍数：论文里章节标题通常明显大于正文，留 10% 余量吸收取整误差。
_HEADING_SIZE_RATIO = 1.10

# 论文里最常见的一级章节名：这些行只要字号大于正文就按一级标题处理，不必参与字号
# 排名——排版常让「Abstract」之类用比「Introduction」更小的字号，纯排名会把它压到
# 三四级标题上去。
_KNOWN_HEADINGS = (
    "abstract", "introduction", "related work", "background",
    "materials and methods", "methods", "method", "results",
    "discussion", "conclusions", "conclusion", "limitations",
    "references", "acknowledgements", "acknowledgments", "supplementary",
)

# 编号章节：「1 Introduction」「2.1 Datasets」「3.4.1 Foo」——编号点数即层级。
_NUMBERED_HEADING = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+\S")
_KNOWN_HEADING = re.compile(r"^(?:" + "|".join(_KNOWN_HEADINGS) + r")\b", re.IGNORECASE)

_cache: OrderedDict[tuple[str, int, int], "PdfText"] = OrderedDict()
_cache_lock = threading.Lock()


@dataclass(frozen=True)
class PdfText:
    """一个 PDF 的本地抽取结果。

    ``blocks`` 是唯一真相源：它既给出正文，也给出每块在原页上的位置（索引侧据此给
    检索块标注页码与坐标）。``body`` 是它的投影——不单独存一份，避免两处不一致。

    Attributes:
        title: 论文标题；元数据与首页启发式都拿不到时为空串。
        pages: 页数。
        blocks: 正文按渲染块拆开的明细（阅读顺序）。
    """

    title: str
    pages: int
    blocks: tuple["Block", ...] = ()

    @property
    def body(self) -> str:
        """正文 markdown 文本（各渲染块按空行连接）。"""
        return "\n\n".join(b.text for b in self.blocks)


@dataclass(frozen=True)
class Block:
    """一个已渲染的 markdown 块（一段正文或一个标题行）及其在原页上的位置。

    Attributes:
        text: 块文本（标题行带 ``#`` 前缀；段落内部用换行连接）。
        page: 页码，1 起（PDF 页序）。
        left: 包围盒左边界（点，向下取整）。
        right: 包围盒右边界。
        top: 包围盒上边界。
        bottom: 包围盒下边界。

    边界条件：跨行合并的标题取其首行的页码，包围盒取各行的并集。
    """

    text: str
    page: int
    left: int
    right: int
    top: int
    bottom: int


@dataclass
class _Line:
    """页面上的一行文本。

    Attributes:
        text: 行文字（连续空白已折叠）。
        size: 行内最大字号，用于判断它是不是章节标题。
        page: 页码，1 起。
        left: 行包围盒左边界（点，向下取整）。
        right: 行包围盒右边界。
        top: 行包围盒上边界。
        bottom: 行包围盒下边界。
    """

    text: str
    size: float
    page: int
    left: int
    right: int
    top: int
    bottom: int


def _block_of(text: str, lines: list[_Line]) -> Block:
    """把一段文字连同它覆盖的那些行合成一个带位置的块。

    Args:
        text: 块的 markdown 文本。
        lines: 组成该块的行（非空）。

    Returns:
        Block: 位置取各行的并集，页码取首行。
    """
    return Block(
        text=text, page=lines[0].page,
        left=min(ln.left for ln in lines), right=max(ln.right for ln in lines),
        top=min(ln.top for ln in lines), bottom=max(ln.bottom for ln in lines),
    )


# ---------- 标题 ----------

#: 标题的最短字符数：短于此的行/字段几乎不可能是论文标题（页眉、页码、栏目名）。
_TITLE_MIN_CHARS = 8

#: 一眼就知道「不是论文标题」的后缀（元数据标题常被写成文件名）。
_TITLE_BAD_SUFFIXES = (".pdf", ".dvi", ".tex", ".docx", ".doc", ".ps", ".html")

#: 一眼就知道「不是论文标题」的整行形态。元数据字段常被生成工具写进工具名、
#: 占位词或打印系统的页码戳；这类值当标题用会同时污染每块前缀与引用匹配。
_TITLE_JUNK_RE = (
    re.compile(r"^untitled$", re.IGNORECASE),
    re.compile(r"^microsoft word\s*[-–—]\s*", re.IGNORECASE),
    re.compile(r"^\d+$"),                                        # 纯数字
    re.compile(r"^[A-Z]{2,}[-_][A-Z0-9]{3,}\s+\d+\.{2,}\d+$"),   # 页码戳（OP-CBIO220080 2246..2253）
    re.compile(r"^arxiv\s*:", re.IGNORECASE),                    # arXiv 页眉
    re.compile(r"^(?:journal|proceedings|volume|vol\.|issue)\b", re.IGNORECASE),
    re.compile(r"^https?://", re.IGNORECASE),
)

#: 出版流程水印/声明字样（子串匹配）：出现在候选里就判它不是标题。
_TITLE_WATERMARKS = (
    "journal pre-proof", "accepted manuscript", "this is a preprint",
    "downloaded from", "all rights reserved", "see discussions",
    "cc-by", "cc by", "©",
)

#: 作者行的标志：姓名标记（∗ † ‡）、iD 标识、上标数字。
_AUTHOR_MARK_RE = re.compile(r"[∗*†‡§]|\biD\b|\bdoi\b|[\u00b9\u00b2\u00b3\u2074-\u2079]")


def _looks_like_author_line(text: str) -> bool:
    """判断一行是不是作者署名行。

    作者行的形态：带姓名标记（∗/†/iD/上标数字）且是逗号分隔的多个短人名串。
    要求「有标记」是为了不误伤标题——标题里出现逗号很常见，但几乎不会带上标
    数字或姓名符号。

    Args:
        text: 候选行文本。

    Returns:
        bool: 像作者署名为 True。
    """
    if _AUTHOR_MARK_RE.search(text) is None:
        return False
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) < 2:
        return False
    # 每个逗号段都以大写字母开头、且短到像人名（不超过 5 个词）
    return all(p[:1].isupper() and len(p.split()) <= 5 for p in parts)


def _looks_like_title(text: str) -> bool:
    """判断一段文本是否够格当论文标题（形态判据，宁空勿错）。

    判据只看「明确不是标题」的形态：占位词、文件名、页码戳、页眉/期刊行、
    水印声明、作者署名行、纯数字。过了判据不代表一定是标题，只是没有被这些
    形态排除——所以调用方在拿不到候选时返回空串，绝不用别的字段顶替。

    Args:
        text: 候选文本。

    Returns:
        bool: 通过判据为 True。
    """
    candidate = " ".join(text.split())
    if len(candidate) < _TITLE_MIN_CHARS:
        return False
    # 文件名/路径：元数据标题常被生成工具写成文件位置
    if candidate.lower().endswith(_TITLE_BAD_SUFFIXES) or "/" in candidate or "\\" in candidate:
        return False
    if any(rx.search(candidate) for rx in _TITLE_JUNK_RE):
        return False
    low = candidate.lower()
    if any(mark in low for mark in _TITLE_WATERMARKS):
        return False
    return not _looks_like_author_line(candidate)


def _raw_metadata_title(doc) -> str:
    """读 PDF 元数据里的标题原文（连续空白折叠）。

    Args:
        doc: 已打开的 PDF 文档对象。

    Returns:
        str: 原始标题；字段为空或读不出来时为空串。
    """
    try:
        raw = (doc.metadata or {}).get("title") or ""
    except Exception:
        # 元数据读不出来不该影响正文——标题退化为启发式即可
        return ""
    return " ".join(raw.split())


def _heuristic_title(doc) -> str:
    """从首页版面猜标题：字号最大、位置最靠上的那组连续同字号行。

    标题在版面上换行是常态（长标题跨两三行），只取一行会把标题截断，所以先按
    「字号相同 + 相邻行」把页面上部的行分组成候选，再逐组过形态判据，取**第一个
    通过的**。分组不要求同块——PyMuPDF 常把每行拆成独立块，按块分组就合并不了
    换行标题；改用「下一行起始位置在上一样高的两倍以内」判相邻，版面上隔开的
    无关同字号行（页眉与期刊名）自然被切开。

    候选按字号降序、位置升序依次试，**不通过判据的组跳过、继续试下一组**：页眉、
    水印这类东西往往字号最大，跳过后仍可能拿到真正的标题；全都不过才返回空。
    这仍然是「宁空勿错」——「空」留给一组候选都没有的情况，而不是被一个坏候选堵死。

    候选只收**明显大于正文字号**的行（与章节标题判据同一口径）：不加这道下限，
    跳过一个坏候选后会一路退到正文行，把一句话当标题——那正是错标题的来路。

    Args:
        doc: 已打开的 PDF 文档对象。

    Returns:
        str: 猜到的标题；没有候选或候选全部不过形态判据时为空串。
    """
    if doc.page_count == 0:
        return ""
    page = doc[0]
    page_height = page.rect.height or 1.0
    body_size = _dominant_size(_page_lines(page, 1))

    # 逐块、逐行收集页面上部且足够长的行（阅读顺序），按字号与相邻性分组
    groups: list[list[tuple[float, float, str]]] = []   # [(字号, y, 文本), …]
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:      # 非文本块（图片等）没有文字可作标题
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            y_top = spans[0].get("bbox", (0, 0, 0, 0))[1]
            size = max(float(s.get("size", 0.0)) for s in spans)
            text = " ".join("".join(s.get("text", "") for s in spans).split())
            if not text or len(text) < _TITLE_MIN_CHARS:
                continue
            # 标题几乎只出现在页面上部；限高可避免把正文里的粗体小标题当成标题
            if y_top > page_height * 0.4:
                continue
            # 字号下限：不大于正文的行走不到这里（正文行当标题是错标题的主要来源）
            if body_size > 0 and size <= body_size * _HEADING_SIZE_RATIO:
                continue
            if groups:
                last = groups[-1][-1]
                same_size = abs(last[0] - size) <= 0.5
                # 相邻：下一行的起点在上一样高的两倍以内（换行标题的续行满足，
                # 版面上隔开的另一处同字号文本不满足）
                adjacent = y_top - last[1] <= 2.0 * max(size, 1.0)
                if same_size and adjacent:
                    groups[-1].append((size, y_top, text))
                    continue
            groups.append([(size, y_top, text)])

    # 字号最大者优先，同字号取最靠上的一组；逐个试判据，第一个过判据的即答案
    for group in sorted(groups, key=lambda g: (-round(g[0][0], 1), g[0][1])):
        candidate = " ".join(text for _size, _y, text in group)
        if _looks_like_title(candidate):
            return candidate
    return ""


def _title_of(doc) -> str:
    """从已打开的文档决定标题（``_extract`` 与轻路径出口共用这一处判据）。

    元数据里**有**标题时，就由它的形态判据定生死——判为垃圾也不退回版面启发式：
    这类 PDF 是工具生成的，版面启发式多半只会捞到页眉或水印，宁可空着。空标题是
    安全降级（调用方跳过它即可），错标题不是——它会同时进每块前缀与引用匹配。

    Args:
        doc: 已打开的 PDF 文档对象。

    Returns:
        str: 论文标题；拿不准时为空串。
    """
    raw = _raw_metadata_title(doc)
    if raw:
        return raw if _looks_like_title(raw) else ""
    return _heuristic_title(doc)


# ---------- 正文 ----------

def _page_lines(page, page_no: int) -> list[_Line]:
    """按阅读顺序取出页内所有文本行。

    顺序沿用 PyMuPDF 的块顺序（即版面阅读顺序），不做坐标重排——双栏论文按坐标
    重排会把左右栏交错成一行左一行右，读起来就散了。

    Args:
        page: PyMuPDF 的页面对象。
        page_no: 该页的页码（1 起），随行带回供索引侧标注检索块位置。

    Returns:
        list[_Line]: 该页的文本行（含包围盒）。
    """
    lines: list[_Line] = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:      # type=0 为文本块，其余是图片等
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            text = " ".join("".join(s.get("text", "") for s in spans).split())
            if not text:
                continue
            # 行包围盒取 PyMuPDF 给出的 (x0, y0, x1, y1)；缺失时给零盒（位置随之为 0，
            # 属于「位置未知」而非错误坐标）。
            x0, y0, x1, y1 = line.get("bbox") or (0, 0, 0, 0)
            lines.append(_Line(
                text=text, size=max(float(s.get("size", 0.0)) for s in spans),
                page=page_no, left=int(x0), top=int(y0),
                right=int(x1), bottom=int(y1),
            ))
    return lines


def _dominant_size(lines: list[_Line]) -> float:
    """取正文字号：按字符量（而非行数）加权投票。

    按字符量加权是因为正文占绝大多数文字；若按行数，密集的短行（页码、参考文献
    条目、图注）可能反客为主，把正文字号选小、导致正文行被误判成标题。

    Args:
        lines: 全文的文本行。

    Returns:
        float: 正文字号；无文本时为 0。
    """
    weight: dict[float, int] = {}
    for line in lines:
        if line.size <= 0:
            continue
        key = round(line.size, 1)
        weight[key] = weight.get(key, 0) + len(line.text)
    if not weight:
        return 0.0
    return max(weight, key=lambda k: weight[k])


def _is_heading_candidate(text: str, size: float, body_size: float, title: str) -> bool:
    """判断一行是否「具备章节标题的形态」（只判形态，不决定级别）。

    判据：字号明显大于正文、行足够短、不是列表项、不是论文标题本身。

    列表项要挡掉，是因为把项目符号映射成字母的 PDF（符号被解成 ``r`` 之类）会让整段
    加粗条目在字号上冒充标题。判定用「首个词是单个小写字母」——那几乎不可能是标题的
    开头，却正是这类错位符号的样子；以 ``A``/``I`` 开头是英文标题的常见写法，不能一概
    按单字母拦掉。

    Args:
        text: 行文字。
        size: 行字号。
        body_size: 正文字号。
        title: 已确定的论文标题（用于剔除正文里重复出现的标题行）。

    Returns:
        bool: 具备标题形态为 True。
    """
    if body_size <= 0 or size <= body_size * _HEADING_SIZE_RATIO:
        return False
    if len(text) > _MAX_HEADING_CHARS:
        return False
    first = text.split(" ", 1)[0]
    if len(first) == 1 and first.isalpha() and first.islower():
        return False                                  # 项目符号被映射成字母的首词
    if text[0] in "•·▪◦‣∙" or text[:2] in ("- ", "* ", "– ", "— "):
        return False                                  # 常见的项目符号
    if title:
        lowered = text.lower()
        if lowered in title.lower() or title.lower() in lowered:
            return False                              # 正文里的论文标题行，顶部已单独输出
    return True


def _rank_levels(lines: list[_Line], body_size: float, title: str) -> dict[float, int]:
    """算出「字号 → 标题级别」的兜底映射：候选标题的字号按从大到小依次排级。

    只在**通过形态判据的行**里排名，否则被挡掉的那些（加粗列表项等）会白白占掉一级，
    把真正的二级标题挤到三级去。``#`` 留给 read_pdf 在文档顶部输出的论文标题，所以这里
    从 ``##`` 起；最深到四级，再深一律用四级表示——论文的章节层级很少超过三层，无止境
    下探只会把噪声也排成 ``######``。

    Args:
        lines: 全文的文本行。
        body_size: 正文字号。
        title: 论文标题。

    Returns:
        dict[float, int]: 字号（保留一位小数）到标题级别的映射；无标题时为空字典。
    """
    sizes = sorted(
        {round(ln.size, 1) for ln in lines
         if _is_heading_candidate(ln.text, ln.size, body_size, title)},
        reverse=True,
    )
    return {size: min(2 + i, 4) for i, size in enumerate(sizes)}


def _heading_level(text: str, size: float, body_size: float,
                   rank: dict[float, int], title: str) -> int | None:
    """给出这一行的 markdown 标题级别；不是标题时为 None。

    级别优先看行本身携带的结构信息——编号章节按编号点数定级（``2.1`` 是三级），常见
    一级章节名固定为二级；都没有才退回按字号排名取级。

    Args:
        text: 行文字。
        size: 行字号。
        body_size: 正文字号。
        rank: 字号到级别的兜底映射（``_rank_levels`` 的结果）。
        title: 论文标题。

    Returns:
        int | None: 标题级别（2~4）；不是标题时为 None。
    """
    if not _is_heading_candidate(text, size, body_size, title):
        return None
    numbered = _NUMBERED_HEADING.match(text)
    if numbered:
        return min(2 + numbered.group(1).count("."), 4)
    if _KNOWN_HEADING.match(text):
        return 2
    return rank.get(round(size, 1))


def _render(lines: list[_Line], body_size: float, rank: dict[float, int],
            title: str) -> list[Block]:
    """把一页的文本行拼成 markdown 块：标题独占一块，连续正文行合成一段。

    标题在版面上换行成多行时字号级别都一样，且中间不夹正文行，据此把它们合并回
    一块——否则一个标题会碎成两三个独立标题。

    **合并有长度上限**：正文段落若整段用了略大于正文字号的字体，每一行都会「像标题」
    地连成一大片；不设上限时它们会合并成一个上千字符的「标题」。超过上限的候选段
    一律当正文——真正的标题不会那么长。

    Args:
        lines: 该页的文本行（阅读顺序）。
        body_size: 正文字号。
        rank: 字号到级别的兜底映射。
        title: 论文标题。

    Returns:
        list[Block]: 该页的 markdown 块（含各自的版面位置）。
    """
    blocks: list[Block] = []
    paragraph: list[_Line] = []
    run: list[_Line] = []                       # 进行中的「同级别同字号候选行」连续段
    run_key: tuple[int, float] | None = None

    def flush_paragraph() -> None:
        if paragraph:
            blocks.append(_block_of("\n".join(ln.text for ln in paragraph), paragraph))
            paragraph.clear()

    def flush_run() -> None:
        """候选段收尾：够短就当标题块，超长则降级成正文块。"""
        nonlocal run, run_key
        if not run:
            return
        text = " ".join(ln.text for ln in run)
        if len(text) <= _MAX_HEADING_CHARS:
            blocks.append(_block_of(f"{'#' * run_key[0]} {text}", run))
        else:
            blocks.append(_block_of("\n".join(ln.text for ln in run), run))
        run, run_key = [], None

    for line in lines:
        level = _heading_level(line.text, line.size, body_size, rank, title)
        if level is None:
            flush_run()                          # 中间出现正文即打断标题的续行合并
            paragraph.append(line)
            continue
        key = (level, round(line.size, 1))
        if run and key == run_key:
            run.append(line)
            continue
        # 新的候选段开始：先按原文顺序把在进行的段落与候选段收尾
        flush_paragraph()
        flush_run()
        run, run_key = [line], key
    flush_paragraph()
    flush_run()
    return blocks


def _extract(path: str) -> PdfText:
    """打开并抽取一个 PDF（不走缓存）。

    Args:
        path: PDF 文件绝对路径。

    Returns:
        PdfText: 抽取结果。

    Raises:
        FileNotFoundError: 路径不存在。
        Exception: 文件损坏、加密或不是 PDF 时按原样上抛（由调用方转为错误结果）。
    """
    import fitz

    # C 层遇到畸形字体/编码会往 stderr 刷告警，糊住终端输出；这里只静音 C 层。
    fitz.TOOLS.mupdf_display_errors(False)
    with fitz.open(path) as doc:
        title = _title_of(doc)
        pages = [_page_lines(page, i + 1) for i, page in enumerate(doc)]

        # 正文字号与级别映射跨页统一：同一篇论文里同级标题字号一致，
        # 逐页各算一套会把同一级标题在不同页排出不同级别。
        all_lines = [line for page in pages for line in page]
        body_size = _dominant_size(all_lines)
        rank = _rank_levels(all_lines, body_size, title)
        blocks = [blk for page in pages for blk in _render(page, body_size, rank, title)]
        return PdfText(title=title, pages=doc.page_count, blocks=tuple(blocks))


def extract_pdf(path: str) -> PdfText:
    """抽取 PDF 文本，按 (绝对路径, 修改时间, 文件大小) 做进程内缓存。

    缓存键带上修改时间与文件大小，文件被替换后自动失效。与 RAG 服务的解析缓存刻意
    分开：那把缓存由索引与检索共用、且解析全程持全局锁（并发读会互相排队），读一篇
    PDF 不该受它影响。

    Args:
        path: PDF 文件路径（可含 ~，可为相对路径）。

    Returns:
        PdfText: 抽取结果；命中缓存时直接返回上次结果。

    Raises:
        FileNotFoundError: 路径不存在。
        Exception: 解析失败时按原样上抛。
    """
    resolved = Path(path).expanduser().resolve()
    stat = resolved.stat()              # 不存在即抛 FileNotFoundError，调用方据此走容错分支
    key = (str(resolved), stat.st_mtime_ns, stat.st_size)

    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)     # 命中即刷新为最近使用，配合下面的容量淘汰
            return cached

    result = _extract(str(resolved))

    with _cache_lock:
        _cache[key] = result
        _cache.move_to_end(key)
        while len(_cache) > _CACHE_MAX_ENTRIES:
            _cache.popitem(last=False)
    return result


def pdf_title(path: str) -> str:
    """只取一个 PDF 的标题（轻路径：读元数据 + 首页版面，不抽全文）。

    与 `extract_pdf` 共用同一处标题判据（`_title_of`），所以两条路取到的标题
    必然一致——为一个标题去抽整篇正文是纯浪费，而两处各写一套判据迟早会漂移。
    不缓存：只读首页，代价远小于整篇抽取，缓存反而要处理失效。

    Args:
        path: PDF 文件路径（可含 ~，可为相对路径）。

    Returns:
        str: 标题；判据不过时为空串（宁空勿错，见 `_looks_like_title`）。

    Raises:
        FileNotFoundError: 路径不存在。
        Exception: 文件损坏、加密或不是 PDF 时按原样上抛。
    """
    import fitz

    fitz.TOOLS.mupdf_display_errors(False)
    resolved = Path(path).expanduser().resolve()
    with fitz.open(str(resolved)) as doc:
        return _title_of(doc)
