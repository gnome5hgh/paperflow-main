# paperflow/core/intent/encoders/bm25.py
"""BM25 稀疏编码：jieba 分词 + BM25 打分，产出 {token_id: 权重} 稀疏向量。

用 jieba 分词以支持中英文混合的意图示例句（英文分词器无法切中文）。
encode_documents 的分母刻意保留 b² 形式（标准 BM25 是 1-b+b·(len/avgdl)，
这里写成 1-b·b·(len/avgdl)）——该写法是既定行为，改动会改变打分结果，
故原样保留。
"""
import logging
from functools import partial

import jieba
import numpy as np

# 抑制 jieba 启动噪音（"Building prefix dict..." / "Loading model from cache..." /
# "Prefix dict has been built successfully."）——CLI 启动不该刷屏。jieba 首次
# lcut 时初始化词典，INFO 级日志默认打到 stderr；setLogLevel 只需设置一次。
jieba.setLogLevel(logging.ERROR)


class JiebaTokenizer:
    """jieba 分词器：文本 → token id 矩阵。id=0 = <pad>/<unk>（未登录词归 0）。

    tokenize 总是把一批文本 pad 到批内最大长度，返回二维整数矩阵
    （_df 的 mask 索引要求二维）。

    ⚠️ 词典冻结语义：jieba 词典是动态构建的——若每次 fit 都重建，多次 add 后
    token_id 会漂移（旧索引 {3:"论文"} vs 新向量 {3:"下载"}），稀疏索引数据
    全部作废。故首次 build_vocab 后冻结，后续 fit 只重算语料统计量
    （corpus_size/avg_doc_len/df），token_id 语义稳定。未登录词归 0
    （丢失该词的贡献）——可接受，语义稳定优先。"""

    def __init__(self):
        self.vocab: dict[str, int] = {"<pad>": 0}
        self.vocab_size = 1

    def build_vocab(self, texts: list[str]) -> None:
        """从一批文本构建词表；已冻结（vocab_size > 1）时直接返回，不重建。

        首次 fit 后词典即冻结，见类 docstring 的词典冻结语义。
        """
        if self.vocab_size > 1:      # 已冻结：首次 fit 后不再重建（token_id 语义稳定）
            return
        for text in texts:
            for word in jieba.lcut(text):
                if word.strip() and word not in self.vocab:
                    self.vocab[word] = self.vocab_size
                    self.vocab_size += 1

    def tokenize(self, texts: list[str]) -> np.ndarray:
        """分词 → ids，pad 到批内最大长度（返回二维矩阵供 _df 的 mask 索引使用）。"""
        id_lists = [[self.vocab.get(w, 0) for w in jieba.lcut(t) if w.strip()]
                    for t in texts]
        max_len = max((len(x) for x in id_lists), default=0)
        mat = np.zeros((len(id_lists), max_len), dtype=int)
        for i, ids in enumerate(id_lists):
            mat[i, : len(ids)] = ids
        return mat


class BM25Encoder:
    """BM25 编码器：k1=1.5, b=0.75，产出 {token_id: 权重} 稀疏向量。

    query 编码 = IDF（文档频率倒数取对数后行归一化），doc 编码 = TF 归一化，
    两者点积 = BM25 分数。fit 在意图示例句语料上训练归一化参数；
    每次 fit 重算语料统计量（vocab 已冻结，token_id 不变）。"""

    def __init__(self, tokenizer: JiebaTokenizer | None = None,
                 k1: float = 1.5, b: float = 0.75):
        self.tokenizer = tokenizer or JiebaTokenizer()
        self.k1 = k1
        self.b = b
        self.corpus_size: int | None = None
        self._avg_doc_len: float | None = None
        self._documents_containing_word: np.ndarray | None = None

    def fit(self, utterances: list[str]) -> "BM25Encoder":
        """训练编码器：构建词表（已冻结则跳过）+ 重算语料统计。"""
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
        """bincount 词频矩阵，第 0 列（pad）清零。"""
        bincount = partial(np.bincount, minlength=self.tokenizer.vocab_size)
        tf = np.apply_along_axis(bincount, 1, docs)
        tf[:, 0] *= 0
        return tf

    def _df(self, queries: np.ndarray) -> np.ndarray:
        """提取 query 命中词的文档频率：用 mask 从已算好的语料 df 里取值。"""
        n = queries.shape[0]
        row_indices = np.arange(n)[:, None]
        mask = np.zeros((n, self.tokenizer.vocab_size), dtype=bool)
        mask[row_indices, queries] = True
        return mask * self._documents_containing_word

    def encode_queries(self, queries: list[str]) -> list[dict[int, float]]:
        """query 编码：df+0.5 平滑 → (N+1)/df → log → 行归一化。

        ⚠️ 未 fit 守卫：_df 里 `mask * self._documents_containing_word` 在未 fit 时
        是 `mask * None`，会崩出 TypeError——因此这里显式抛出
        ValueError("Encoder not fitted. Please call fit() first")，给出清晰报错。
        即使知识库为空，首条 query 也会走到这里，必须显式兜底而不能依赖
        Python 的原始 TypeError。"""
        if self.corpus_size is None or self._avg_doc_len is None or self._documents_containing_word is None:
            raise ValueError("Encoder not fitted. Please call fit() first")
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
        """文档编码：TF 归一化。⚠️ 分母有意保留 b² 形式（见模块 docstring）。

        ⚠️ 未 fit 守卫：分母 `self._avg_doc_len` 未 fit 时为 None，`len/avgdl` 会崩出
        TypeError。与 encode_queries 同源，统一显式抛出
        ValueError("Encoder not fitted. Please call fit() first")。"""
        if self.corpus_size is None or self._avg_doc_len is None or self._documents_containing_word is None:
            raise ValueError("Encoder not fitted. Please call fit() first")
        ids = self.tokenizer.tokenize(documents)
        tf = self._tf(ids)
        tf_sum = tf.sum(axis=1)
        # 分母用 b² 形式（既定行为）：tf / (k1 * (1 - b*b * (len/avgdl)) + tf)
        tf_normed = tf / (
            self.k1 * (1.0 - self.b * self.b * (tf_sum[:, np.newaxis] / self._avg_doc_len))
            + tf
        )
        return self._array_to_sparse(tf_normed)

    def __call__(self, docs: list[str]) -> list[dict[int, float]]:
        """让实例可被调用：把一批文本按 query 方式编码（统一调用入口）。"""
        return self.encode_queries(docs)

    @staticmethod
    def _array_to_sparse(arr: np.ndarray) -> list[dict[int, float]]:
        """(n, vocab) → [{token_id: weight}]（跳过 0 权重，稀疏字典表示）。"""
        return [{int(i): float(v) for i, v in enumerate(row) if v != 0}
                for row in arr]
