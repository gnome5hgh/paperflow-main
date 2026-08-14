"""把 PDF 解析成结构化章节：优先用 GROBID 服务（返回 TEI XML），
GROBID 不可用时改用 PyMuPDF 做启发式解析。

GrobidClient 刻意不做 SSRF 防护校验：它的地址是固定的本地可信端点，
不是用户输入。若需要纵深防御，可在上层调用网络工具时显式传入
allowlist={"127.0.0.1:8070"} 做精确匹配。
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

#: TEI 命名空间
_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


@dataclass
class ParsedDoc:
    """PDF 解析结果：章节列表、表格文本、图片说明文本。"""

    sections: list[tuple[str, str]]   # (标题, 正文) 列表
    tables: list[str]
    figures: list[str]


class GrobidClient:
    """GROBID 服务的 HTTP 客户端，负责可用性探测与 PDF 全文解析。"""

    def __init__(self, url: str = "http://127.0.0.1:8070", transport=None, timeout: float = 60.0):
        """配置服务地址并创建 HTTP 客户端。

        transport 供测试注入自定义传输层（如 MockTransport）；timeout 是
        请求超时秒数，健康检查与解析共用。
        """
        self.url = url.rstrip("/")
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def available(self) -> bool:
        """探测服务是否可用：请求健康检查接口，异常或超时都视为不可用。"""
        try:
            r = self._client.get(f"{self.url}/api/isalive")
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    def extract_title(self, path: str) -> str | None:
        """提取 PDF 论文标题：走 GROBID header 接口，返回 TEI 里的 <title>。

        GROBID 0.8 按 Accept 头协商输出格式——必须显式要 XML，否则默认回
        BibTeX（见 /api/processHeaderDocument 的格式协商行为）。
        标题链里 GROBID 是可降级层：网络/服务/解析任一步失败都返回 None 而不
        抛错，让 TitleExtractor 落到 LLM 层兜底（与 parse_pdf 抛错的契约不同
        ——那是解析链路必经步骤，错误必须上抛给调用方决定）。
        """
        try:
            with open(path, "rb") as f:
                r = self._client.post(
                    f"{self.url}/api/processHeaderDocument",
                    files={"input": f},
                    params={"consolidateHeader": "1"},
                    headers={"Accept": "application/xml"},
                )
            r.raise_for_status()
            root = ET.fromstring(r.text)
        except (httpx.HTTPError, OSError, ET.ParseError):
            return None
        # 优先取 type="main" 的标题：期刊文章的 header 里 analytic/monogr 都带
        # title，首个 main 才是论文标题本身；找不到再退到第一个 <title>。
        title = root.find(".//tei:title[@type='main']", _TEI_NS)
        if title is None:
            title = root.find(".//tei:title", _TEI_NS)
        return ((title.text if title is not None else None) or "").strip() or None

    def parse_pdf(self, path: str) -> ParsedDoc:
        """把本地 PDF 文件提交给 GROBID 做全文解析，返回结构化章节。

        网络或服务出错时会抛出异常（不吞错，由调用方决定下一步怎么处理）。
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

        遍历所有 div 块，提取每块的标题、正文段落，以及块内的表格和图片说明。
        """
        root = ET.fromstring(xml)
        sections: list[tuple[str, str]] = []
        tables: list[str] = []
        figures: list[str] = []
        for div in root.iter("{http://www.tei-c.org/ns/1.0}div"):
            head = div.find("tei:head", _TEI_NS)
            if head is not None and head.text:
                heading = head.text
            else:
                # GROBID 的 abstract 等 div 常无 <head>，改用 div@type 作标题
                # （如 type="abstract"），保证首段有可读 heading。
                heading = div.get("type", "") or ""
            paras = [p.text or "" for p in div.findall("tei:p", _TEI_NS)]
            if paras:
                sections.append((heading, "\n".join(paras)))
            for t in div.findall("tei:table", _TEI_NS):
                tables.append("".join(t.itertext()))
            for f in div.findall("tei:figure", _TEI_NS):
                cap = f.find(".//tei:figDesc", _TEI_NS)
                figures.append(cap.text if cap is not None and cap.text else "")
        return ParsedDoc(sections=sections, tables=tables, figures=figures)


class PyMuPDFParser:
    """GROBID 不可用时的备用解析器：用 PyMuPDF 抽取全文，按字号粗略切分章节（精度够切块用即可）。"""

    def parse_pdf(self, path: str) -> ParsedDoc:
        """抽取 PDF 全文并按字号启发式切分章节，返回结构化的章节列表。"""
        import fitz
        doc = fitz.open(path)
        sections: list[tuple[str, str]] = [("", "")]
        for page in doc:
            d = page.get_text("dict")
            for block in d.get("blocks", []):
                if block.get("type") != 0:   # 跳过图片块
                    continue
                for line in block.get("lines", []):
                    size = line["spans"][0]["size"] if line["spans"] else 0
                    text = "".join(s["text"] for s in line["spans"]).strip()
                    if not text:
                        continue
                    # 字号 ≥ 12 视为标题行（启发式），开启新 section
                    if size >= 12 and sections[-1][1]:
                        sections.append((text, ""))
                    else:
                        sections[-1] = (sections[-1][0], sections[-1][1] + text + "\n")
        return ParsedDoc(sections=[(h, t) for h, t in sections if t], tables=[], figures=[])
