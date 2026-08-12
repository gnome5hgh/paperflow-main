# tests/test_rag_reranker.py
from paperflow.rag.encoders.reranker import FakeReranker


def test_fake_reranker_deterministic():
    r = FakeReranker()
    docs = ["apple", "banana", "cherry"]
    assert r("query", docs, top_k=2) == r("query", docs, top_k=2)
    assert len(r("query", docs, top_k=2)) == 2


def test_fake_reranker_bge_lazy(monkeypatch):
    import paperflow.rag.encoders.reranker as mod

    class FakeCE:
        def __init__(self, *a, **k): pass
        def predict(self, pairs, **k):
            # 与 docs 顺序一致的分数：pair 为 [query, doc]，取第二元素即 doc 长度
            return [float(len(d)) for _, d in pairs]

    monkeypatch.setattr(mod, "CrossEncoder", FakeCE)
    e = mod.BgeReranker(model_name="fake")
    assert e._model is None
    order = e("q", ["a", "bbbb", "ccc"], top_k=2)
    assert order == [1, 2]      # 最长文档分数最高
