# paperflow/core/intent/bm25_encoder.py
"""BM25 稀疏编码——对齐 semantic_router/encoders/bm25.py。

唯一替换：PretrainedTokenizer("bert-base-uncased") → JiebaTokenizer（中英文意图 utterance）。
⚠️ encode_documents 严格保留 0.1.16 的 b*b 公式（源码笔误，决策 A 保留——与 0.1.16 行为一致）。
"""
from functools import partial

import jieba
import numpy as np


class JiebaTokenizer:
    """对齐 PretrainedTokenizer 接口。id=0 = <pad>/<unk>（OOV 归 0）。
    tokenize 总是 pad 到批内最大长度，返回 2D int 矩阵（_df 的 mask 索引要求 2D）。

    ⚠️ vocab 冻结语义（jieba 替换 bert 的隐藏差异修复）：
    bert 的 vocab 预训练固定（30522，永不改变）；jieba 是动态构建——
    若每次 fit 重建 vocab，多次 add 后 token_id 漂移（旧索引 {3:"论文"} vs
    新向量 {3:"下载"}），稀疏索引数据作废。故：首次 build_vocab 后冻结，
    后续 fit 只重算统计量（corpus_size/avg_doc_len/df），token_id 语义稳定。
    新词 OOV 归 0（丢失贡献）——可接受，语义稳定优先（对齐 bert 固定 vocab）。"""

    def __init__(self):
        self.vocab: dict[str, int] = {"<pad>": 0}
        self.vocab_size = 1

    def build_vocab(self, texts: list[str]) -> None:
        if self.vocab_size > 1:      # 已冻结：首次 fit 后不再重建（token_id 语义稳定）
            return
        for text in texts:
            for word in jieba.lcut(text):
                if word.strip() and word not in self.vocab:
                    self.vocab[word] = self.vocab_size
                    self.vocab_size += 1

    def tokenize(self, texts: list[str]) -> np.ndarray:
        """分词 → ids，pad 到批内最大长度（对齐 PretrainedTokenizer.tokenize）。"""
        id_lists = [[self.vocab.get(w, 0) for w in jieba.lcut(t) if w.strip()]
                    for t in texts]
        max_len = max((len(x) for x in id_lists), default=0)
        mat = np.zeros((len(id_lists), max_len), dtype=int)
        for i, ids in enumerate(id_lists):
            mat[i, : len(ids)] = ids
        return mat


class BM25Encoder:
    """对齐 BM25Encoder。k1=1.5, b=0.75。

    核心：query 编码 = IDF（左半），doc 编码 = TF 归一化（右半），
    两者点积 = BM25 分数。fit 在 utterance 语料上训练归一化参数。
    fit 每次重算统计量（vocab 已冻结，token_id 不变）。"""

    def __init__(self, tokenizer: JiebaTokenizer | None = None,
                 k1: float = 1.5, b: float = 0.75):
        self.tokenizer = tokenizer or JiebaTokenizer()
        self.k1 = k1
        self.b = b
        self.corpus_size: int | None = None
        self._avg_doc_len: float | None = None
        self._documents_containing_word: np.ndarray | None = None

    def fit(self, utterances: list[str]) -> "BM25Encoder":
        """对齐 fit()：构建 vocab（冻结后跳过）+ 重算语料统计。"""
        self.tokenizer.build_vocab(utterances)
        ids = self.tokenizer.tokenize(utterances)
        corpus = self._tf(ids)
        self.corpus_size = len(utterances)
        self._avg_doc_len = float(corpus.sum(axis=1).mean())
        df = np.atleast_2d((corpus > 0).sum(axis=0))
        df[:, 0] *= 0                            # 忽略 pad
        self._documents_containing_word = df
        return self

    def _tf(self, docs: np.ndarray) -> np.ndarray:
        """对齐 _tf()：bincount 词频矩阵，第 0 列（pad）清零。"""
        bincount = partial(np.bincount, minlength=self.tokenizer.vocab_size)
        tf = np.apply_along_axis(bincount, 1, docs)
        tf[:, 0] *= 0
        return tf

    def _df(self, queries: np.ndarray) -> np.ndarray:
        """对齐 _df()：mask 提取 query 命中词的文档频率。"""
        n = queries.shape[0]
        row_indices = np.arange(n)[:, None]
        mask = np.zeros((n, self.tokenizer.vocab_size), dtype=bool)
        mask[row_indices, queries] = True
        return mask * self._documents_containing_word

    def encode_queries(self, queries: list[str]) -> list[dict[int, float]]:
        """对齐 encode_queries()：df+0.5 平滑 → (N+1)/df → log → 行归一化。"""
        ids = self.tokenizer.tokenize(queries)
        df = self._df(ids)
        df = df + np.where(df > 0, 0.5, 0)
        idf = np.divide(self.corpus_size + 1, df,
                        out=np.zeros_like(df), where=df != 0)
        idf = np.log(idf, out=np.zeros_like(df), where=df != 0)
        idf_norm = np.divide(idf, idf.sum(axis=1)[:, np.newaxis],
                             out=np.zeros_like(idf), where=idf != 0)
        return self._array_to_sparse(idf_norm)

    def encode_documents(self, documents: list[str]) -> list[dict[int, float]]:
        """对齐 encode_documents()：TF 归一化。⚠️ 保留 0.1.16 的 b*b 公式（决策 A）。"""
        ids = self.tokenizer.tokenize(documents)
        tf = self._tf(ids)
        tf_sum = tf.sum(axis=1)
        # 严格复现源码：tf / (k1 * (1 - b*b * (len/avgdl)) + tf)
        tf_normed = tf / (
            self.k1 * (1.0 - self.b * self.b * (tf_sum[:, np.newaxis] / self._avg_doc_len))
            + tf
        )
        return self._array_to_sparse(tf_normed)

    def __call__(self, docs: list[str]) -> list[dict[int, float]]:
        return self.encode_queries(docs)

    @staticmethod
    def _array_to_sparse(arr: np.ndarray) -> list[dict[int, float]]:
        """(n, vocab) → [{token_id: weight}]（跳过 0，对齐 SparseEmbedding 语义）。"""
        return [{int(i): float(v) for i, v in enumerate(row) if v != 0}
                for row in arr]
