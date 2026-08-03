"""Stage 0 实体提取测试：确定性正则，五类实体命中/不误报。"""
from paperflow.core.intent.entities import extract_entities


def test_extract_pdf_path():
    assert extract_entities("读 /Users/me/vault/pdf/circrna.pdf 总结一下")["pdf_path"] == \
        "/Users/me/vault/pdf/circrna.pdf"


def test_extract_arxiv_id():
    assert extract_entities("搜索 2401.12345 的论文")["arxiv_id"] == "2401.12345"


def test_extract_doi():
    assert extract_entities("这个 DOI 10.1000/xyz123 是哪篇")["doi"] == "10.1000/xyz123"


def test_extract_note_path():
    assert extract_entities("看 /Users/me/vault/note/foo.md 里写了什么")["note_path"] == \
        "/Users/me/vault/note/foo.md"


def test_extract_figure_zh_and_en():
    assert extract_entities("解释 Figure 3 的机制")["figure"] == "3"
    assert extract_entities("图 5 说明了什么")["figure"] == "5"


def test_no_entities_returns_empty():
    assert extract_entities("这篇论文关于什么") == {}


def test_relative_pdf_not_matched():
    """相对路径不算实体（WorkspacePolicy 要求绝对路径；且兼容既有 stub 断言）。"""
    assert "pdf_path" not in extract_entities("下载 paper/pdf/x.pdf")
