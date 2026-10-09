"""本地 PDF 文本抽取（PyMuPDF）：把一个 PDF 读成 markdown 文本，不依赖任何外部服务。

与 ``paperflow/rag/parsers/`` 的分工：那边是带结构化语义（TEI 章节、表格、书目元数据）
的解析，服务向量库索引与语料标题索引；这里只做「把 PDF 读成可读文本」这一件事，供
read_pdf 工具使用。两条路径刻意互不牵连——读一篇论文既不该依赖 GROBID 服务是否健康，
也不该排在索引/检索共用的那把全局锁后面等。

输出形态由版面推断：PyMuPDF 只给出带字号与坐标的文本行，本模块按字号识别章节标题并
分级，其余行按原文换行合成段落。文本顺序**沿用 PyMuPDF 给出的块顺序**——那是版面的
阅读顺序，双栏论文靠它才是「先左栏到底、再右栏」；按坐标重排反而会把两栏交错。

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

    Attributes:
        title: 论文标题；元数据与首页启发式都拿不到时为空串。
        body: 正文 markdown 文本（章节标题由字号推断并分级）。
        pages: 页数。
    """

    title: str
    body: str
    pages: int


@dataclass
class _Line:
    """页面上的一行文本。

    Attributes:
        text: 行文字（连续空白已折叠）。
        size: 行内最大字号，用于判断它是不是章节标题。
    """

    text: str
    size: float


# ---------- 标题 ----------

def _metadata_title(doc) -> str:
    """读 PDF 元数据里的标题，明显不可用时返回空串。

    很多 PDF 的 title 字段被生成工具写成了文件名、路径甚至工具名，这类值当标题用
    会污染笔记头部与引用标注，所以只接受看起来像标题的值，其余交给首页启发式。

    Args:
        doc: 已打开的 PDF 文档对象。

    Returns:
        str: 可用的元数据标题；不可用时为空串。
    """
    try:
        raw = (doc.metadata or {}).get("title") or ""
    except Exception:
        # 元数据读不出来不该影响正文——标题退化为启发式即可
        return ""
    title = " ".join(raw.split())
    if len(title) < 8:
        return ""
    if title.lower().endswith(".pdf") or "/" in title or "\\" in title:
        return ""
    return title


def _heuristic_title(doc) -> str:
    """从首页版面猜标题：字号最大、位置最靠上的那一行。

    标题提取在本项目另有权威链路（搜索元数据 > GROBID > 大模型 > pdftitle > PyMuPDF
    启发式，见记忆层的 TitleExtractor）。这里不复用那条链路：工具层不该反向依赖记忆
    服务，而且此处只需要「连元数据都没有时」的兜底，能用即可。

    Args:
        doc: 已打开的 PDF 文档对象。

    Returns:
        str: 猜到的标题；无从判断时为空串。
    """
    if doc.page_count == 0:
        return ""
    page = doc[0]
    page_height = page.rect.height or 1.0
    best_size = 0.0
    best_text = ""
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:      # 非文本块（图片等）没有文字可作标题
            continue
        for line in block.get("lines", []):
            spans = line.get("spans", [])
            if not spans:
                continue
            # 标题几乎只出现在页面上部；限高可避免把正文里的粗体小标题当成标题
            y_top = spans[0].get("bbox", (0, 0, 0, 0))[1]
            if y_top > page_height * 0.4:
                continue
            size = max(float(s.get("size", 0.0)) for s in spans)
            text = " ".join("".join(s.get("text", "") for s in spans).split())
            if len(text) < 8:
                continue
            if size > best_size:
                best_size, best_text = size, text
    return best_text


# ---------- 正文 ----------

def _page_lines(page) -> list[_Line]:
    """按阅读顺序取出页内所有文本行。

    顺序沿用 PyMuPDF 的块顺序（即版面阅读顺序），不做坐标重排——双栏论文按坐标
    重排会把左右栏交错成一行左一行右，读起来就散了。

    Args:
        page: PyMuPDF 的页面对象。

    Returns:
        list[_Line]: 该页的文本行。
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
            lines.append(_Line(text=text, size=max(float(s.get("size", 0.0)) for s in spans)))
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


def _render(lines: list[_Line], body_size: float, rank: dict[float, int], title: str) -> str:
    """把一页的文本行拼成 markdown：标题独占一行，连续正文行合成段落。

    同一个标题在版面上换行成多行时字号级别都一样，且中间不会夹正文行，据此把它们
    合并回一行——否则一个标题会碎成两三个独立标题。

    Args:
        lines: 该页的文本行（阅读顺序）。
        body_size: 正文字号。
        rank: 字号到级别的兜底映射。
        title: 论文标题。

    Returns:
        str: 该页的 markdown 文本。
    """
    blocks: list[str] = []
    paragraph: list[str] = []
    prev_heading: tuple[int, float] | None = None

    def flush() -> None:
        if paragraph:
            blocks.append("\n".join(paragraph))
            paragraph.clear()

    for line in lines:
        level = _heading_level(line.text, line.size, body_size, rank, title)
        if level is None:
            paragraph.append(line.text)
            prev_heading = None          # 中间出现正文即打断标题的续行合并
            continue
        flush()
        key = (level, round(line.size, 1))
        if prev_heading == key:
            blocks[-1] = f"{blocks[-1]} {line.text}"
        else:
            blocks.append(f"{'#' * level} {line.text}")
        prev_heading = key
    flush()
    return "\n\n".join(blocks)


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
        title = _metadata_title(doc) or _heuristic_title(doc)
        pages = [_page_lines(page) for page in doc]

        # 正文字号与级别映射跨页统一：同一篇论文里同级标题字号一致，
        # 逐页各算一套会把同一级标题在不同页排出不同级别。
        all_lines = [line for page in pages for line in page]
        body_size = _dominant_size(all_lines)
        rank = _rank_levels(all_lines, body_size, title)
        body = "\n\n".join(
            part for part in (
                _render(page, body_size, rank, title) for page in pages
            ) if part
        )
        return PdfText(title=title, body=body, pages=doc.page_count)


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
