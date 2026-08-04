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


def test_extract_pdf_path_with_spaces():
    """RC2a 回归：vault 目录/文件名含空格是常态，实体必须能提取（修复前 [^\s:] 排除空白返回空）。"""
    path = "/Users/me/vault/pdf/Heterogeneous graph/Variational Disentangled Graph Auto-Encoders for Link Prediction.pdf"
    assert extract_entities(f"读 {path} 生成笔记")["pdf_path"] == path


def test_extract_preserves_double_space():
    """RC2b 前提：双空格文件名必须逐字保留（修复前正则不匹配，取不到）。"""
    path = "/Users/me/vault/pdf/a  b.pdf"
    assert extract_entities(f"读 {path}")["pdf_path"] == path


def test_extract_coexisting_pdf_and_md_independent():
    """多实体共存契约：pdf+md 同行各自独立提取——初稿 [^\n]*? 会让后出现者被
    前者 / 起点吸收，分段式正则修复。"""
    entities = extract_entities("根据 /a/doc.pdf 更新 /b/note.md")
    assert entities["pdf_path"] == "/a/doc.pdf"
    assert entities["note_path"] == "/b/note.md"
