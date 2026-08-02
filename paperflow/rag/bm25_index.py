"""BM25 检索：rank_bm25 + jieba（中文分词）英文空格切分。

与 core/intent/bm25_encoder.py 的分工：本模块是生产 rank_bm25；
intent 侧是冻结词表确定性复现（路由数学验证专用）。二者刻意的，勿合并。

权威性：BM25 是 ChromaDB 的内存投影（非独立权威副本）。进程重启即失，
重建源为 ChromaDB documents（见 indexer）；增量更新必须与向量库同步
（delete-then-reindex 双写），否则投影漂移。
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
    """内存投影索引：dict 幂等，同 doc_id 覆盖而非追加，支持逐文档移除。

    用 dict 而非常规 list：chunk id 与内容无关（sha1(path:idx)），
    热更新编辑会让同 id 换内容，必须覆盖；note 收缩删除旧位置块，必须移除。
    """

    def __init__(self):
        self._docs: dict[str, list[str]] = {}
        self._bm25: BM25Okapi | None = None

    def _ensure(self) -> None:
        """惰性构造 BM25Okapi：dict 变更（rebuild/add/remove）后置 None，首次查询再建。"""
        if self._bm25 is None:
            self._bm25 = BM25Okapi(list(self._docs.values())) if self._docs else None

    def rebuild(self, items: list[tuple[str, str]]) -> None:
        """全量重建：(doc_id, text) 列表（通常来自 ChromaDB documents）。"""
        self._docs = {did: tokenize(t) for did, t in items}
        self._bm25 = None

    def add_documents(self, items: list[tuple[str, str]]) -> None:
        """增量 upsert：同 doc_id 覆盖而非追加（幂等，修累积/双填充）。

        旧实现盲目 list.append 会导致 BM25 随编辑次数膨胀；改为 dict 赋值
        天然去重。惰性重建 BM25Okapi（版本无关、实现简单）。"""
        for did, text in items:
            self._docs[did] = tokenize(text)
        self._bm25 = None

    def remove_document(self, doc_id: str) -> None:
        """单文档移除（文档级 delete-then-reindex 用，与向量库删除成对）。"""
        self._docs.pop(doc_id, None)
        self._bm25 = None

    def query(self, text: str, top_k: int) -> list[str]:
        if not self._docs:
            return []
        self._ensure()
        assert self._bm25 is not None
        scores = self._bm25.get_scores(tokenize(text))
        ids = list(self._docs.keys())           # dict 插入序与 BM25Okapi 对齐
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [ids[i] for i in order[:top_k]]

    def is_empty(self) -> bool:
        return len(self._docs) == 0

    def count(self) -> int:
        """块数（供测试断言幂等性，与向量库 count 对齐）。"""
        return len(self._docs)
