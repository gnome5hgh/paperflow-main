"""把 PDF 解析成结构化章节：优先用 GROBID 服务（返回 TEI XML），
GROBID 不可用时改用 PyMuPDF 做启发式解析。

GrobidClient 刻意不做 SSRF 防护校验：它的地址是固定的本地可信端点，
不是用户输入。若需要纵深防御，可在上层调用网络工具时显式传入
allowlist={"127.0.0.1:8070"} 做精确匹配。
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

from paperflow.rag.constants import GROBID_TIMEOUT

#: TEI 命名空间（GROBID 返回的 XML 使用该命名空间）
_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


@dataclass
class ParsedDoc:
    """PDF 解析结果：章节列表、表格文本、图片说明文本。

    Attributes:
        sections: 章节列表，每个元素为 (标题, 正文) 的二元组。
        tables: 所有表格的纯文本内容列表（提取自 <table> 标签）。
        figures: 所有图片的说明文本列表（提取自 <figDesc> 标签）。
        title: TEI header 提取的主标题（corpus 索引用），无 header 时为空串。
        biblio: 文献书目元数据（建 bib 条目用），键为
            authors/year/journal/volume/number/pages，缺失的键不出现。
    """

    sections: list[tuple[str, str]]   # (标题, 正文) 列表
    tables: list[str]
    figures: list[str]
    title: str = ""                   # TEI header 主标题（corpus 索引用）
    # biblio 是可变默认值，dataclass 不允许直接写 {}，须用 default_factory
    biblio: dict = field(default_factory=dict)   # 文献书目元数据（建 bib 条目用）


class GrobidClient:
    """GROBID 服务的 HTTP 客户端，负责可用性探测与 PDF 全文解析。
    本客户端封装了与 GROBID REST API 的交互，包括健康检查、标题提取和全文解析。
    """

    def __init__(self, url: str = "http://127.0.0.1:8070", transport=None, timeout: float = GROBID_TIMEOUT):
        """配置服务地址并创建 HTTP 客户端。

        Args:
            url: GROBID 服务的基础 URL，末尾有无斜杠均可。
            transport: httpx 传输层对象，用于测试时注入 MockTransport 等。
            timeout: 所有 HTTP 请求的超时时间（秒），包括健康检查和解析请求。
        """
        self.url = url.rstrip("/")
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def available(self) -> bool:
        """探测服务是否可用：请求健康检查接口，异常或超时都视为不可用。

        Returns:
            bool: True 表示服务可用，False 表示不可用。
        """
        # 通过请求 `/api/isalive` 接口判断服务状态。所有网络异常（如连接拒绝、超时）或非 200 响应均视为不可用。
        try:
            r = self._client.get(f"{self.url}/api/isalive")
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    def extract_title(self, path: str) -> str | None:
        """提取 PDF 论文标题：走 GROBID header 接口，返回 TEI 里的 <title>。

        调用 GROBID 的 `/api/processHeaderDocument` 接口，专门解析文献头部信息。
        此方法设计为可降级：网络/服务/解析任一步失败都返回 None 而不抛错
        让上层（如 TitleExtractor）回退到 LLM 层兜底。

        注意：GROBID 0.8 按 Accept 头协商输出格式，必须显式指定 `application/xml`，
             否则默认返回 BibTeX 格式，导致解析失败（见 /api/processHeaderDocument 的格式协商行为）。

        Args:
            path: PDF 文件的本地路径。

        Returns:
            str | None: 提取到的标题文本（已去除首尾空白），失败时返回 None。
        """
        # 优先查找 `<tei:title type="main">`，这是文献的主标题；若不存在则回退到第一个 `<tei:title>` 标签。
        try:
            with open(path, "rb") as f:
                r = self._client.post(
                    f"{self.url}/api/processHeaderDocument",
                    files={"input": f},
                    params={"consolidateHeader": "1"},     # 合并作者/机构信息
                    headers={"Accept": "application/xml"}, # 强制返回 XML
                )
            r.raise_for_status()
            root = ET.fromstring(r.text)
        except (httpx.HTTPError, OSError, ET.ParseError):
            return None
        # 优先查找 `<tei:title type="main">`，这是文献的主标题（期刊文章的 header 里可能包含多个 title）
        title = root.find(".//tei:title[@type='main']", _TEI_NS)

        if title is None:
            # 若不存在则回退到第一个 `<tei:title>` 标签。
            title = root.find(".//tei:title", _TEI_NS)

        # 若找到标签则取其文本，否则返回 None；空字符串或仅空白也视为 None
        return ((title.text if title is not None else None) or "").strip() or None

    def parse_pdf(self, path: str) -> ParsedDoc:
        """把本地 PDF 文件提交给 GROBID 做全文解析，返回结构化章节。

        调用 `/api/processFulltextDocument` 接口进行完整文档解析。
        与 `extract_title` 不同，此方法在失败时会抛出异常（不吞错），
        因为全文解析是检索链路的关键步骤，上层需要知道失败原因并决定如何处理。

        Args:
            path: PDF 文件的本地路径。

        Returns:
            ParsedDoc: 解析后的结构化文档对象，包含章节、表格和图片说明。

        Raises:
            httpx.HTTPError: 网络请求失败或服务返回非 2xx 状态码。
            ET.ParseError: GROBID 返回的 XML 格式不符合预期。
            OSError: 无法打开本地 PDF 文件。
        """
        with open(path, "rb") as f:
            r = self._client.post(
                f"{self.url}/api/processFulltextDocument",
                files={"input": f},
                params={"consolidateHeader": "1"},
            )
        r.raise_for_status()
        return self._parse_tei(r.text)

    def _parse_tei(self, xml: str) -> ParsedDoc:
        """把 GROBID 返回的 TEI XML 解析成 ParsedDoc。

        解析策略：
        - 遍历所有 `<tei:div>` 块（代表文档中的章节/节）。
        - 对每个 div，提取 `<tei:head>` 作为标题；若无 head，则用 div 的 `type` 属性
          （如 "abstract"）作为标题，确保每个章节都有可读的标题。
        - 提取所有 `<tei:p>` 段落，拼接成正文。
        - 表格与图注做全文档遍历收集：GROBID 把它们放在 body 直属的 `<tei:figure>`
          下（`<tei:table>` 又嵌套在 figure 里），并不在 `<tei:div>` 之下，
          按 div 直接子节点找会全部漏掉。

        Args:
            xml: GROBID 返回的完整 TEI XML 字符串。

        Returns:
            ParsedDoc: 解析结果。
        """
        root = ET.fromstring(xml)
        # 从 TEI header 提取主标题与书目元数据（无 header 时为空值，不抛错）
        title, biblio = self._extract_header(root)
        sections: list[tuple[str, str]] = []
        tables: list[str] = []
        figures: list[str] = []

        for div in root.iter("{http://www.tei-c.org/ns/1.0}div"):
            # 提取标题：优先用 <head>，否则用 div@type
            head = div.find("tei:head", _TEI_NS)
            if head is not None and head.text:
                heading = head.text
            else:
                # GROBID 的 abstract 等 div 常无 <head>，改用 div@type 作标题
                # （如 type="abstract"），保证首段有可读 heading。
                heading = div.get("type", "") or ""

            # 提取所有段落的纯文本
            paras = [p.text or "" for p in div.findall("tei:p", _TEI_NS)]
            if paras:
                sections.append((heading, "\n".join(paras)))

        # 表格与图注：GROBID 把二者放在 body 直属的 <figure> 下（<table> 又嵌套在
        # <figure> 里），不是 <div> 的直接子节点——必须全文档遍历，才拿得到内容。
        for tbl in root.iter(f"{{{_TEI_NS['tei']}}}table"):
            tables.append("".join(tbl.itertext()))
        for fig in root.iter(f"{{{_TEI_NS['tei']}}}figure"):
            cap = fig.find(".//tei:figDesc", _TEI_NS)
            # figDesc 可能含子标记（此时 .text 为 None），故用 itertext 汇总全部文本
            figures.append("".join(cap.itertext()).strip() if cap is not None else "")

        return ParsedDoc(sections=sections, tables=tables, figures=figures,
                         title=title, biblio=biblio)

    def _extract_header(self, root):
        """从 TEI header 提取主标题与书目元数据，任一层缺失返回空值不抛错。"""
        header = root.find("tei:teiHeader", _TEI_NS)
        if header is None:
            return "", {}
        title = ""
        t = header.find("tei:fileDesc/tei:titleStmt/tei:title", _TEI_NS)
        if t is not None and t.text:
            title = t.text.strip()
        biblio = {}
        bs = header.find("tei:fileDesc/tei:sourceDesc//tei:biblStruct", _TEI_NS)
        if bs is not None:
            authors = []
            for a in bs.iter(f"{{{_TEI_NS['tei']}}}author"):
                s = a.find(".//tei:surname", _TEI_NS)
                if s is not None and s.text:
                    authors.append(s.text.strip())
            if authors:
                biblio["authors"] = ", ".join(authors)
            for tag, key in (("tei:title[@level='j']", "journal"),
                             ("tei:date", "year")):
                el = bs.find(f".//{tag}", _TEI_NS)
                if el is not None and (el.text or el.get("when")):
                    biblio[key] = (el.text or el.get("when")).strip()
            for unit, key in (("volume", "volume"), ("issue", "number"), ("page", "pages")):
                scope = bs.find(f".//tei:biblScope[@unit='{unit}']", _TEI_NS)
                if scope is not None and scope.text:
                    biblio[key] = scope.text.strip()
        return title, biblio


class PyMuPDFParser:
    """GROBID 不可用时的备用解析器

    使用 PyMuPDF (fitz) 从 PDF 中提取文本，并通过字号大小启发式地划分章节。
    精度虽不如 GROBID，但对于后续的文本分块（chunking）已足够。
    """

    def parse_pdf(self, path: str) -> ParsedDoc:
        """抽取 PDF 全文并按字号启发式切分章节，返回结构化的章节列表。

        算法思路：
        - 使用 PyMuPDF 的 `get_text("dict")` 获取页面文本的详细布局信息，包括每个文本块的坐标、字号和内容。
        - 遍历所有文本块，跳过非文本块（如图片）。
        - 对于每个文本行，检查其字号：
          - 如果字号 ≥ 12 且当前已累积正文内容，则认为这是一个新的章节标题，开启一个新章节。
          - 否则，将当前行追加到当前章节的正文中。
        - 初始化时默认有一个空标题的章节，用于收集开头的正文。

        边界条件：
        - 只保留有正文内容的章节（忽略仅有标题而无正文的部分）。
        - 字号阈值 12 是经验值，适用于多数学术论文的正文/标题区分。

        Args:
            path: PDF 文件的本地路径。

        Returns:
            ParsedDoc: 解析结果，其中 tables 和 figures 列表为空（备用解析不支持）。
        """
        import fitz
        fitz.TOOLS.mupdf_display_errors(False)  # C 层 stderr 告警不糊屏
        doc = fitz.open(path)
        # 初始化一个空标题章节，用于容纳开头的正文
        sections: list[tuple[str, str]] = [("", "")]
        for page in doc:
            d = page.get_text("dict")
            for block in d.get("blocks", []):
                if block.get("type") != 0:   # type=0 表示文本块，其他为图片等
                    continue
                for line in block.get("lines", []):
                    # 取本行第一个 span 的字号（假设一行内字号一致）
                    size = line["spans"][0]["size"] if line["spans"] else 0
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if not text:
                        continue
                    # # 若字号大（≥12）且当前章节已有正文，则作为新章节标题（启发式），开启新 section
                    if size >= 12 and sections[-1][1]:
                        sections.append((text, ""))
                    else:
                        # 追加到当前章节正文
                        sections[-1] = (sections[-1][0], sections[-1][1] + text + "\n")
        # 过滤掉没有正文的章节
        return ParsedDoc(sections=[(h, t) for h, t in sections if t], tables=[], figures=[])
