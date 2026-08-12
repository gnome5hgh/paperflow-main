import numpy as np
import pytest
from paperflow.core.intent.encoders.bm25 import JiebaTokenizer, BM25Encoder


class TestJiebaTokenizer:
    def test_vocab_builds(self):
        tok = JiebaTokenizer()
        tok.build_vocab(["circRNA 海绵 机制"])
        assert tok.vocab_size > 1
        assert "<pad>" in tok.vocab

    def test_vocab_frozen_after_first_build(self):
        tok = JiebaTokenizer()
        tok.build_vocab(["circRNA 海绵"])
        size1 = tok.vocab_size
        first_ids = tok.tokenize(["circRNA 海绵"])[0]
        tok.build_vocab(["异构图 神经网络 链路 预测 新词 表"])   # 第二次 build 应被忽略
        assert tok.vocab_size == size1                      # 冻结：不新增
        second_ids = tok.tokenize(["circRNA 海绵"])[0]
        np.testing.assert_array_equal(first_ids, second_ids)  # token_id 语义稳定

    def test_oov_maps_to_zero(self):
        tok = JiebaTokenizer()
        tok.build_vocab(["circRNA"])
        ids = tok.tokenize(["完全不认识的词xyz"])[0]
        assert (ids == 0).all() or (ids[0] == 0)   # OOV 归 0（<pad>/<unk>）

    def test_tokenize_pads_to_batch_max(self):
        tok = JiebaTokenizer()
        tok.build_vocab(["短 语", "这个句子比较长 一些"])
        mat = tok.tokenize(["短 语", "这个句子比较长 一些"])
        assert mat.shape[0] == 2
        assert mat.shape[1] == mat[1].size or mat.shape[1] >= mat[0].size


class TestBM25Encoder:
    def test_fit_sets_statistics(self):
        enc = BM25Encoder()
        enc.fit(["circRNA 海绵 机制", "异构图 神经网络 链路预测"])
        assert enc.corpus_size == 2
        assert enc._avg_doc_len > 0
        assert enc._documents_containing_word is not None

    def test_encode_documents_preserves_b_b_bug(self):
        """决策 A：b*b（b=0.75 → 0.5625）必须保留——手算验证。"""
        enc = BM25Encoder()
        enc.fit(["circRNA 海绵", "circRNA 机制"])
        # 用短文档避免平均长度除零：单 token 文档
        sparse = enc.encode_documents(["circRNA"])
        # 手算：tf=1, len=1, avgdl 来自 fit 语料（2 文档各 2 token → avgdl=2）
        # tf_normed = 1 / (1.5 * (1 - 0.5625 * (1/2)) + 1) = 1 / (1.5*0.71875 + 1) = 1/2.078125
        expected = 1.0 / (1.5 * (1.0 - 0.75 * 0.75 * (1.0 / 2.0)) + 1.0)
        assert abs(list(sparse[0].values())[0] - expected) < 1e-6

    def test_encode_queries_returns_sparse_dicts(self):
        enc = BM25Encoder()
        enc.fit(["circRNA 海绵 机制", "异构图 神经网络 链路预测"])
        q = enc.encode_queries(["circRNA"])
        assert isinstance(q[0], dict)
        assert all(isinstance(k, int) and isinstance(v, float) for k, v in q[0].items())

    def test_call_maps_to_queries(self):
        enc = BM25Encoder()
        enc.fit(["circRNA 海绵"])
        out = enc(["circRNA"])
        out2 = enc.encode_queries(["circRNA"])
        assert out == out2

    def test_array_to_sparse_skips_zero(self):
        arr = np.array([[0.0, 0.5, 0.0, 0.25]])
        out = BM25Encoder._array_to_sparse(arr)
        assert out == [{1: 0.5, 3: 0.25}]

    def test_encode_queries_before_fit_raises(self):
        """未 fit 就 encode_queries 必须抛干净的 ValueError（上游 0.1.16 对齐）。
        否则 _df 里 mask * None 会崩 TypeError，Layer 4 空 routes.yaml 首条 query 直接踩中。"""
        enc = BM25Encoder()
        with pytest.raises(ValueError, match="not fitted"):
            enc.encode_queries(["circRNA"])

    def test_encode_documents_before_fit_raises(self):
        """未 fit 就 encode_documents 必须抛干净的 ValueError（上游 0.1.16 对齐）。"""
        enc = BM25Encoder()
        with pytest.raises(ValueError, match="not fitted"):
            enc.encode_documents(["circRNA"])
