"""BM25 关键词检索：用 rank_bm25 库实现，中文用 jieba 分词，英文按空格切分。

注意本索引只驻留在内存里，是向量库中全部文档文本的投影：进程重启后为空，
需要从向量库整体重建；任何文档增删改都要与向量库成对执行，否则内存索引
会和向量库逐渐漂移、不一致。
"""
import jieba
from rank_bm25 import BM25Okapi


def tokenize(text: str) -> list[str]:
    """中英混合分词：jieba 处理中文，英文按空格小写切分。

    Args:
        text: 原始文本（中英文混合）。

    Returns:
        list[str]: 分词后的 token 列表。
    """
    tokens = []

    # 1. 使用 jieba 对中文进行细粒度切词，返回词语列表。
    for seg in jieba.lcut(text):
        # 2. 对每个切出的词语，按空格再次拆分（主要处理 jieba 未识别的英文复合词），并将每个子词转为小写，实现英文的不区分大小写匹配。
        tokens.extend(w.lower() for w in seg.split())
    return tokens


class Bm25Index:
    """内存版 BM25 索引，用 dict 按文档 id 保存分词结果。

    为什么用 dict 而非 list：
    - 文档 id 由路径加序号哈希生成，同一 id 的内容可能被反复编辑覆盖。
    - 若用 list 追加，旧内容会越积越多，导致 BM25 统计失真。
    - 用 dict 按 id 赋值可实现覆盖更新（幂等），按 id 弹出可精确删除。

    Attributes:
        _docs: dict[str, list[str]]，文档 id → 分词列表（按 id 覆盖赋值，幂等）
        _bm25: BM25Okapi | None，惰性构建的索引；_docs 变更后置 None，下次查询重建
    """

    def __init__(self):
        """初始化空索引：_docs 存每个文档 id 的分词结果，_bm25 惰性构建、变更即失效。

        _docs 负责存，_bm25 负责算。_docs 一变，_bm25 立即过期。
        """
        self._docs: dict[str, list[str]] = {} # 键为文档 id，值为该文档的分词列表。
        self._bm25: BM25Okapi | None = None   # rank_bm25 库的索引对象，惰性构建。当 _docs 变更后置为 None，下次查询时通过 _ensure() 重建。

    def _ensure(self) -> None:
        """惰性构建或重建 BM25 索引对象。

        当 _docs 变更（增/删/改）后，_bm25 被置为 None，本方法负责重新构建。
        构建时取 _docs 的 values 列表作为文档集合。
        注意：BM25Okapi 内部按列表顺序存储文档，后续查询取分数时，
        必须使用相同的键顺序（list(self._docs.keys())）来对应文档 id。
        """
        if self._bm25 is None:
            # 调用 self._bm25 = BM25Okapi(list(self._docs.values())) 时，BM25Okapi 内部会立即计算出以下 4 个核心属性：
            # - doc_len：按顺序记录每篇文档的长度（词数）。
            # - avgdl：所有文档的平均长度
            # - doc_freqs：词频矩阵，按文档顺序，记录每个词在该文档中出现了几次。
            # - idf：逆文档频率，统计每个词出现在了多少篇文档。
            self._bm25 = BM25Okapi(list(self._docs.values())) if self._docs else None

    def rebuild(self, items: list[tuple[str, str]]) -> None:
        """重建全部文档的索引，(通常用于进程启动时从向量库恢复）。

        Args:
            items: (文档id, 文档文本) 的列表。
        """
        self._docs = {did: tokenize(t) for did, t in items}
        self._bm25 = None # 使缓存失效，下次查询时重建

    def add_documents(self, items: list[tuple[str, str]]) -> None:
        """批量新增或更新文档（覆盖而非追加），保证幂等。

        为什么是幂等的：
        - 对于已有的 doc_id，赋值操作会覆盖旧的分词列表，而不是追加。
        - 用 dict 按 id 赋值即可去重。
        - 变更后置空索引对象，下次查询时惰性重建。

        Args:
            items: (文档id, 文档文本) 的列表。
        """
        for did, text in items:
            self._docs[did] = tokenize(text)
        self._bm25 = None # 使缓存失效

    def remove_document(self, doc_id: str) -> None:
        """按 id 移除单个文档。

        若 doc_id 不存在则静默忽略（pop 的默认行为）。
        调用方需确保与向量库的删除操作成对执行，否则索引会漂移不一致。

        Args:
            doc_id: 要移除的文档 id。
        """
        self._docs.pop(doc_id, None)
        self._bm25 = None # 使缓存失效

    def query(self, text: str, top_k: int) -> list[str]:
        """检索与 text 最相关的 top_k 个文档 id，按 BM25 分数降序返回。

        Args:
            text: 查询文本。
            top_k: 返回结果数量上限。

        Returns:
            list[str]: 按相关度降序排列的文档 id 列表。
        """
        # 1. 若索引为空，直接返回空列表。
        if not self._docs:
            return []

        # 2. 调用 _ensure() 确保 BM25 对象已构建。
        self._ensure()

        # 3. 对查询文本分词，调用 BM25Okapi.get_scores() 获取所有文档的分数。
        assert self._bm25 is not None
        # 获取所有文档的 BM25 分数（顺序与 self._docs.values() 一致）
        scores = self._bm25.get_scores(tokenize(text))
        # 获取相同顺序的文档 id 列表
        ids = list(self._docs.keys())

        # 5. 按分数降序排序，截取前 top_k 个 id 返回。
        order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [ids[i] for i in order[:top_k]]

    def is_empty(self) -> bool:
        """索引中是否还没有任何文档。

        Returns:
            True 表示索引为空（ BM25 路不可用，检索退纯向量路）。
        """
        return len(self._docs) == 0

    def count(self) -> int:
        """索引中的文档数（供测试断言增量幂等，与向量库计数对齐）。

        Returns:
            已登记的文档条数。
        """
        return len(self._docs)
