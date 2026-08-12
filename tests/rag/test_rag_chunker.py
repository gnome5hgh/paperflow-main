# tests/test_rag_chunker.py
from paperflow.rag.parsers.chunker import AcademicChunker


def test_references_dropped():
    c = AcademicChunker()
    chunks = c.split_doc("note/a.md", [
        ("Introduction", "intro text"),
        ("References", "should be dropped"),
    ], "note")
    assert len(chunks) == 1
    assert chunks[0].heading == "Introduction"


def test_long_section_split_with_overlap():
    c = AcademicChunker(max_tokens=10, overlap_tokens=2)
    long_text = " ".join(f"token{i}" for i in range(30))
    chunks = c.split_doc("pdf/x.pdf", [("Method", long_text)], "pdf")
    # 30 token / (10-2) stride ≈ 4 块
    assert len(chunks) >= 3
    # 相邻块应有重叠（第二块开头包含第一块尾部 token）
    assert chunks[0].text.split()[-1] in chunks[1].text.split()[:3]


def test_chunk_ids_stable_and_indexed():
    c = AcademicChunker()
    chunks = c.split_doc("note/a.md", [("A", "hello"), ("B", "world")], "note")
    assert [k.chunk_index for k in chunks] == [0, 1]
    assert chunks[0].id != chunks[1].id
    assert chunks[0].path == "note/a.md"
    assert chunks[0].source == "note"
