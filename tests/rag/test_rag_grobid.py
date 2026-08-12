# tests/test_rag_grobid.py
import httpx
from paperflow.rag.parsers.grobid_client import GrobidClient, PyMuPDFParser, ParsedDoc

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


_HEADER_TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader><fileDesc><titleStmt>
    <title level="a" type="main">异构图神经网络的权威标题</title>
  </titleStmt></fileDesc></teiHeader>
</TEI>
"""


def test_grobid_available_true():
    c = GrobidClient(transport=_transport())
    assert c.available() is True


def test_grobid_extract_title_from_header(tmp_path):
    """extract_title 返回 TEI 里的 type=main 标题，且必须显式请求 XML 输出。"""
    captured = {}
    def handler(req):
        captured["headers"] = req.headers
        return httpx.Response(200, content=_HEADER_TEI.encode("utf-8"))
    c = GrobidClient(transport=httpx.MockTransport(handler))
    dummy = tmp_path / "dummy.pdf"
    dummy.write_bytes(b"dummy")
    assert c.extract_title(str(dummy)) == "异构图神经网络的权威标题"
    # GROBID 0.8 按 Accept 头协商输出格式：不请求 XML 会回 BibTeX，取不到 <title>
    assert captured["headers"].get("accept") == "application/xml"


def test_grobid_extract_title_none_when_service_down(tmp_path):
    """服务不可达 → 返回 None 不抛错（标题链降级到 LLM 层）。"""
    def handler(req):
        raise httpx.ConnectError("conn refused")
    c = GrobidClient(transport=httpx.MockTransport(handler))
    dummy = tmp_path / "dummy.pdf"
    dummy.write_bytes(b"dummy")
    assert c.extract_title(str(dummy)) is None


def test_grobid_extract_title_none_when_title_empty(tmp_path):
    """TEI 里 title 为空 → 返回 None（GROBID 对个别 PDF 抽不出标题，让出降级）。"""
    tei = _HEADER_TEI.replace("异构图神经网络的权威标题", "")
    def handler(req):
        return httpx.Response(200, content=tei.encode("utf-8"))
    c = GrobidClient(transport=httpx.MockTransport(handler))
    dummy = tmp_path / "dummy.pdf"
    dummy.write_bytes(b"dummy")
    assert c.extract_title(str(dummy)) is None


def test_grobid_extract_title_none_when_title_whitespace(tmp_path):
    """TEI 里 title 为纯空白 → 返回 None 而非空串（契约 str|None）。"""
    tei = _HEADER_TEI.replace("异构图神经网络的权威标题", "   ")
    def handler(req):
        return httpx.Response(200, content=tei.encode("utf-8"))
    c = GrobidClient(transport=httpx.MockTransport(handler))
    dummy = tmp_path / "dummy.pdf"
    dummy.write_bytes(b"dummy")
    assert c.extract_title(str(dummy)) is None


def test_grobid_extract_title_none_when_no_title_element(tmp_path):
    """TEI 里无 <title> 元素 → 返回 None 而非抛异常（失败降级合同）。"""
    tei = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader><fileDesc><titleStmt></titleStmt></fileDesc></teiHeader>
</TEI>
"""
    def handler(req):
        return httpx.Response(200, content=tei.encode("utf-8"))
    c = GrobidClient(transport=httpx.MockTransport(handler))
    dummy = tmp_path / "dummy.pdf"
    dummy.write_bytes(b"dummy")
    assert c.extract_title(str(dummy)) is None


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
