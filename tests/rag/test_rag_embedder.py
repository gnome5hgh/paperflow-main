# tests/test_rag_embedder.py
import numpy as np
from paperflow.rag.encoders.embedder import FakeEmbedder


def test_fake_embedder_deterministic():
    e = FakeEmbedder(dim=64)
    v1 = e(["hello", "world"])
    v2 = e(["hello", "world"])
    assert v1.shape == (2, 64)
    np.testing.assert_array_equal(v1, v2)


def test_fake_embedder_normalized():
    e = FakeEmbedder(dim=16)
    vecs = e(["paper", "论文"])
    norms = np.linalg.norm(vecs, axis=1)
    np.testing.assert_allclose(norms, 1.0, atol=1e-6)


def test_fake_embedder_dim():
    assert FakeEmbedder(dim=128).dim == 128


def test_bge_embedder_lazy_load(monkeypatch):
    # 不加载真实模型：用 stub 替换 sentence_transformers 导入，验证惰性 + 维度读取
    import paperflow.rag.encoders.embedder as mod

    class FakeST:
        def __init__(self, *a, **k):
            self.called = 0

        def get_sentence_embedding_dimension(self):
            return 512

        def encode(self, texts, **kw):
            return np.zeros((len(texts), 512))

    monkeypatch.setattr(mod, "SentenceTransformer", FakeST)
    e = mod.BgeEmbedder(model_name="fake-model")
    assert e._model is None          # 构造不加载
    assert e.dim == 512              # 访问 dim 触发加载 + 从模型读维度
    out = e(["x"])                   # 复用已加载模型
    assert out.shape == (1, 512)
