"""BM25 检索：rank_bm25 + jieba（中文分词）英文空格切分。

与 core/intent/bm25_encoder.py 的分工：本模块是生产 rank_bm25；
intent 侧是冻结词表确定性复现（路由数学验证专用）。二者刻意的，勿合并。
"""
import jieba
from rank_bm25 import BM25Okapi


def tokenize(text: str) -> list[str]:
    """中英混合分词：jieba 处理中文，英文按空格小写切分。"""
    tokens = []
    for seg in jieba.lcut(text):
        tokens.extend(w.lower() for w in seg.split())
    return tokens


class Bm25Index:
    """内存索引，重启即失——重建源为 ChromaDB documents（见 indexer）。"""

    def __init__(self):
        self._tokenized: list[list[str]] = []
        self._doc_ids: list[str] = []
        self._bm25: BM25Okapi | None = None

    def _ensure_bm25(self) -> None:
        if self._bm25 is None:
            self._bm25 = BM25Okapi(self._tokenized)

    def rebuild(self, items: list[tuple[str, str]]) -> None:
        """全量重建：(doc_id, text) 列表（通常来自 ChromaDB documents）。"""
        self._tokenized = [tokenize(t) for _, t in items]
        self._doc_ids = [d for d, _ in items]
        self._bm25 = BM25Okapi(self._tokenized)

    def add_documents(self, items: list[tuple[str, str]]) -> None:
        """热更新增量：追加后整体重建 BM25Okapi（版本无关、实现简单）。

        不用 rank_bm25 的 add_document（0.2.2+ 才有，旧版本缺失）——
        本地单用户场景语料小，重建仅分词，成本可接受。"""
        for doc_id, text in items:
            self._doc_ids.append(doc_id)
            self._tokenized.append(tokenize(text))
        self._bm25 = BM25Okapi(self._tokenized)

    def query(self, text: str, top_k: int) -> list[str]:
        if not self._doc_ids:
            return []
        self._ensure_bm25()
        scores = self._bm25.get_scores(tokenize(text))
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [self._doc_ids[i] for i in order[:top_k]]

    def is_empty(self) -> bool:
        return len(self._doc_ids) == 0
