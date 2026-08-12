import numpy as np
from paperflow.core.intent.encoders.index import HybridLocalIndex


class TestHybridLocalIndex:
    def test_query_single_vector(self):
        idx = HybridLocalIndex()
        idx.add(
            embeddings=[[1.0, 0.0], [0.0, 1.0]],
            routes=["a", "b"],
            utterances=["u1", "u2"],
            sparse_embeddings=[{1: 0.1}, {2: 0.2}],
        )
        scores, names = idx.query(np.array([1.0, 0.0]), top_k=2,
                                  sparse_vector={1: 0.1})
        assert names[0] == "a"          # 与 doc0 完全对齐
        assert scores[0] > scores[1]

    def test_query_empty_index(self):
        idx = HybridLocalIndex()
        scores, names = idx.query(np.array([1.0, 0.0]))
        assert scores.size == 0
        assert names == []

    def test_add_concatenates(self):
        idx = HybridLocalIndex()
        idx.add(embeddings=[[1.0, 0.0]], routes=["a"], utterances=["u1"],
                sparse_embeddings=[{1: 0.5}])
        idx.add(embeddings=[[0.0, 1.0]], routes=["b"], utterances=["u2"],
                sparse_embeddings=[{2: 0.5}])
        assert idx.index.shape == (2, 2)
        assert list(idx.routes) == ["a", "b"]

    def test_sparse_dot_product(self):
        idx = HybridLocalIndex()
        idx.add(embeddings=[[1.0]], routes=["a"], utterances=["u"],
                sparse_embeddings=[{1: 0.5, 2: 0.25}])
        result = idx._sparse_index_dot_product({1: 2.0, 2: 4.0})
        assert abs(result[0] - (2.0 * 0.5 + 4.0 * 0.25)) < 1e-9   # = 2.0

    def test_sparse_dot_product_none_query(self):
        idx = HybridLocalIndex()
        idx.add(embeddings=[[1.0]], routes=["a"], utterances=["u"],
                sparse_embeddings=[{1: 0.5}])
        result = idx._sparse_index_dot_product(None)
        assert result == [0.0]
