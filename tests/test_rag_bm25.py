from paperflow.rag.bm25_index import Bm25Index, tokenize


def test_tokenize_mixed():
    toks = tokenize("circRNA 调控 miRNA 表达")
    assert "circrna" in toks          # 英文小写空格切分
    assert "调控" in toks              # 中文 jieba 分词


def test_rebuild_query_roundtrip():
    b = Bm25Index()
    b.rebuild([
        ("d1", "circRNA 与 miRNA 调控网络"),
        ("d2", "drug target interaction prediction"),
        ("d3", "unrelated topic here"),
    ])
    hits = b.query("circRNA", top_k=1)
    assert hits == ["d1"]


def test_add_documents_incremental():
    b = Bm25Index()
    b.rebuild([("d1", "hello world"), ("d2", "hello again")])
    b.add_documents([("d3", "circRNA paper")])
    assert b.query("circRNA", top_k=1) == ["d3"]


def test_is_empty():
    assert Bm25Index().is_empty() is True
    b = Bm25Index()
    b.rebuild([("d1", "hello")])
    assert b.is_empty() is False
