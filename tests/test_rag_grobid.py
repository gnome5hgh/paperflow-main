# tests/test_rag_grobid.py
import httpx
from paperflow.rag.grobid_client import GrobidClient, PyMuPDFParser, ParsedDoc

_TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader></teiHeader>
  <text><body>
    <div type="abstract"><p>Abstract text.</p></div>
    <div type="section"><head>Introduction</head><p>Intro body.</p></div>
    <div type="section"><head>References</head><p>Refs body.</p></div>
  </body></text></TEI>
"""


def _transport():
    return httpx.MockTransport(lambda req: httpx.Response(
        200, content=_TEI.encode("utf-8")))


def test_grobid_available_true():
    c = GrobidClient(transport=_transport())
    assert c.available() is True


def test_grobid_parse_pdf_extracts_sections(tmp_path):
    # MockTransport 不读取文件内容；实现里 open(path, "rb") 只要求文件存在。
    # 用 tmp_path 写占位文件，避免依赖全局 /tmp/dummy.pdf（brief 原文有此隐患）。
    dummy = tmp_path / "dummy.pdf"
    dummy.write_bytes(b"dummy")
    c = GrobidClient(transport=_transport())
    doc = c.parse_pdf(str(dummy))
    assert doc.sections[0][0].lower() == "abstract"
    assert "Intro body" in doc.sections[1][1]
    # References 段不丢弃（丢弃是 chunker 的职责）
    assert len(doc.sections) == 3


def test_pymupdf_parser_fallback(tmp_path):
    # pymupdf 动态生成一个小 PDF
    import fitz
    p = tmp_path / "tiny.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Hello Paragraph")
    doc.save(str(p))
    doc.close()
    parsed = PyMuPDFParser().parse_pdf(str(p))
    assert "Hello Paragraph" in "".join(t for _, t in parsed.sections)
