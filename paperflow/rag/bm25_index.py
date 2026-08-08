"""BM25 关键词检索：用 rank_bm25 库实现，中文用 jieba 分词，英文按空格切分。

与意图识别模块里另一套"冻结词表、确定性复现"的 BM25 不同，这里才是生产
环境实际使用的检索实现，两者刻意分开维护，不要合并。

注意本索引只驻留在内存里，是向量库中全部文档文本的投影：进程重启后为空，
需要从向量库整体重建；任何文档增删改都要与向量库成对执行，否则内存索引
会和向量库逐渐漂移、不一致。
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
    """内存版 BM25 索引，用 dict 按文档 id 保存分词结果。

    用 dict 而非 list 的原因：文档 id 由路径加序号哈希生成、与内容无关，
    同一 id 的内容可能被反复编辑覆盖，新增时必须覆盖旧值而不是追加；
    文档被删除时也能按 id 精确移除对应条目。这样索引始终是幂等的。
    """

    def __init__(self):
        # _docs 保存 {文档id: 分词列表}；_bm25 是惰性构建的索引对象，
        # 任何变更后都会置空，待下次查询时再重建。
        self._docs: dict[str, list[str]] = {}
        self._bm25: BM25Okapi | None = None

    def _ensure(self) -> None:
        """惰性构造索引对象：文档集合变更后置空，首次查询时才重建。"""
        if self._bm25 is None:
            self._bm25 = BM25Okapi(list(self._docs.values())) if self._docs else None

    def rebuild(self, items: list[tuple[str, str]]) -> None:
        """全量重建索引，入参是 (文档id, 文本) 列表（通常来自向量库的全部文档）。"""
        self._docs = {did: tokenize(t) for did, t in items}
        self._bm25 = None

    def add_documents(self, items: list[tuple[str, str]]) -> None:
        """新增或更新文档：同一 id 覆盖旧内容而不是追加，天然幂等。

        若改成在列表末尾追加，同一文档被反复编辑时旧内容会越积越多，
        导致 BM25 统计失真；用 dict 按 id 赋值即可去重。变更后置空
        索引对象，下次查询时惰性重建。
        """
        for did, text in items:
            self._docs[did] = tokenize(text)
        self._bm25 = None

    def remove_document(self, doc_id: str) -> None:
        """按 id 移除单个文档；调用方需保证与向量库的删除成对执行。"""
        self._docs.pop(doc_id, None)
        self._bm25 = None

    def query(self, text: str, top_k: int) -> list[str]:
        """检索与 text 最相关的 top_k 个文档 id，按 BM25 分数降序返回。"""
        if not self._docs:
            return []
        self._ensure()
        assert self._bm25 is not None
        scores = self._bm25.get_scores(tokenize(text))
        # 注意：scores 的下标对应构造时传入的文档列表顺序，而该列表由
        # dict 的值生成，因此这里用 dict 的插入序取 id，两者必须对齐。
        ids = list(self._docs.keys())
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [ids[i] for i in order[:top_k]]

    def is_empty(self) -> bool:
        """索引中是否还没有任何文档。"""
        return len(self._docs) == 0

    def count(self) -> int:
        """索引中的文档数（供测试断言增量幂等，与向量库计数对齐）。"""
        return len(self._docs)
