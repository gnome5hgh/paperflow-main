"""GROBID（TEI XML 解析）→ 结构化 section；不可用回退 PyMuPDF 启发式解析。

GrobidClient 刻意跳过 SSRF validate：固定可信本地端点（非用户输入）；
纵深防御可在上层传 allowlist={"127.0.0.1:8070"}（network.py netloc 精确匹配）。
"""
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import httpx

#: TEI 命名空间
_TEI_NS = {"tei": "http://www.tei-c.org/ns/1.0"}


@dataclass
class ParsedDoc:
    sections: list[tuple[str, str]]   # (heading, text) 列表
    tables: list[str]
    figures: list[str]


class GrobidClient:
    def __init__(self, url: str = "http://127.0.0.1:8070", transport=None, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self._client = httpx.Client(transport=transport, timeout=timeout)

    def available(self) -> bool:
        """可用性探测：GET /api/isalive，异常/超时即 False。"""
        try:
            r = self._client.get(f"{self.url}/api/isalive")
            return r.status_code == 200
        except (httpx.HTTPError, OSError):
            return False

    def parse_pdf(self, path: str) -> ParsedDoc:
        with open(path, "rb") as f:
            r = self._client.post(
                f"{self.url}/api/processFulltextDocument",
                files={"input": f},
                params={"consolidateHeader": "1"},
            )
        r.raise_for_status()
        return self._parse_tei(r.text)

    def _parse_tei(self, xml: str) -> ParsedDoc:
        root = ET.fromstring(xml)
        sections: list[tuple[str, str]] = []
        tables: list[str] = []
        figures: list[str] = []
        for div in root.iter("{http://www.tei-c.org/ns/1.0}div"):
            head = div.find("tei:head", _TEI_NS)
            if head is not None and head.text:
                heading = head.text
            else:
                # GROBID 的 abstract 等 div 常无 <head>，回退用 div@type 作标题
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
    """启发式回退：PyMuPDF 抽全文，按字号跳变粗分 section（够 chunker 用即可）。"""

    def parse_pdf(self, path: str) -> ParsedDoc:
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
