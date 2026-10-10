"""检索器：融合 BM25 关键词与向量检索的候选（支持多查询改写集，同一 RRF 池融合），再交给重排模型精排。

索引为空时返回空结果。对外检索工具 RagRetrieveTool 在 `paperflow/tools/rag/rag_retrieve.py`。
"""
import logging

from paperflow.rag.domain import Chunk, indexed_text

logger = logging.getLogger(__name__)

#: 重排候选池随 top_k 放大的倍率（无量纲）：实际池大小为
#: ``max(top_k * 本值, config.rag.retriever.rerank_candidates)``，保证大 top_k
#: 时池子同步放大。改它改变大 top_k 场景的精排候选面与开销，需重评检索质量。
RERANK_CANDIDATE_MULTIPLIER = 2

#: query 侧任务指令（Qwen3-Embedding 官方格式 Instruct: {task}\nQuery: {query}，
#: 只加 query 侧、文档侧不加，官方称可提升 1–5%）。
# 文档编码在索引器完成，不受影响；意图路由复用同一 embedder，也走各自调用、无此前缀。
_QUERY_INSTRUCTION = ("Given an academic research query, retrieve relevant "
                      "passages from papers and reading notes")


class Retriever:
    """融合检索引擎：同时跑 BM25 关键词检索与向量检索，RRF 合并，再精排。

    与检索工具 RagRetrieveTool（tools/rag）是两类职责：这里实现检索与融合算法；
    RagRetrieveTool 只做对外暴露的薄封装（取单例、持锁、格式化结果）。

    Attributes:
        service: RAGService 门面，底层组件经它惰性获取
        _bm25_synced: bool，BM25 进程级同步标记；首次查询前需从向量库整体重建一次
    """

    def __init__(self, service):
        """绑定门面服务：底层组件（向量库/BM25/编码器/重排器）都经 service 惰性获取。

        Args:
            service: RAGService 门面实例，提供配置与各底层组件的惰性获取。
        """
        self.service = service
        # BM25 进程级同步标记：BM25 是内存投影，进程重启即空，首次查询前必须
        # 从向量库整体重建一次才能与向量路对齐；此后增删改由索引器与向量库
        # 成对执行维护，无需再重建。
        self._bm25_synced = False

    def retrieve(self, queries, top_k: int | None = None) -> list[Chunk]:
        """对查询集执行检索：每条 query 独立跑双路，全部排名进同一 RRF 池融合。

        Args:
            queries: 查询集（str 视为单条）——queries[0] 为主查询（reranker
                     用它打分）；其余为改写变体，词面不同、语义等价。
            top_k: 返回块数；None 时取配置 ``rag.retriever.top_k``（默认值单点在
                   config，不再用模块常量字面量）。

        并发约定：调用方（RagRetrieveTool）已持有锁，此处不再加锁——锁由
        工具层统一控制，若在此重复加锁会造成死锁或锁语义混乱（防止后人
        「顺手补锁」的关键 WHY）。

        边界条件：
        - BM25 索引为空 → 只用向量检索；两路均为空 → 返回空列表。
        - 空查询集/全空串 → 归一为 [""]（与空索引组合时返回空列表，不炸）。
        - 重排返回的下标越界 → 安全截断（防御性）。

        Returns:
            精排后的块列表（带完整元数据），最多 top_k 条；无命中时为空列表。
        """
        if isinstance(queries, str):
            queries = [queries]
        # 查询集归一：去空串；全空则占位一条，让下游按「查无此人」自然返回空
        cleaned = [q.strip() for q in queries if q and q.strip()] or [""]
        primary = cleaned[0]

        # top_k 默认值单点在配置（rag.retriever.top_k）；None 时才解析，
        # 显式传入的值（含 0 等边界）原样使用。
        if top_k is None:
            top_k = self.service.config.rag.retriever.top_k

        # 检索阈值读配置（rag.retriever.*）：值的唯一声明点在 config.py。
        rcfg = self.service.config.rag.retriever
        # rrf_k 是用户可配旋钮，无下界保证：排名从 0 起，k=0 时分母为 0
        # 直接 ZeroDivisionError。这里钳到 >=1 兜底。
        rrf_k = max(1, rcfg.rrf_k)

        embedder = self.service._ensure_embedder()
        # 稠密路软降级：云端 embed 失败该次查询退 BM25 独路，
        # 不抛给用户——检索可用性优先于召回完整性，警告进日志。
        try:
            qvecs = embedder([f"Instruct: {_QUERY_INSTRUCTION}\nQuery: {q}"
                              for q in cleaned])
        except Exception as e:
            logger.warning("RAG 查询编码失败，本次退化为纯 BM25 检索：%s", e)
            qvecs = None

        vs = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # ---- BM25 进程级恢复（裸 rebuild）----
        # 每进程同步一次，而非「空了才补」：重启后若先发生一次写热更新，
        # BM25 只含新写的一篇，与向量库已漂移（非空但残缺），is_empty 探测不到。
        # 重建以向量库正文为唯一源，只恢复 BM25 内存索引，
        # 不触碰索引状态与文档块（那是 index_all 全量重扫的职责，个人语料规模下两者开销差一个量级）。
        # 重建失败（Milvus 读取异常）不置位，下次查询重试；本次退化为纯向量路。
        if not self._bm25_synced:
            bm25.rebuild([(c.id, indexed_text(c)) for c, _mtime in vs.all_documents()])
            self._bm25_synced = True

        # ---- 多查询双路检索，全部排名累计进同一 RRF 池 ----
        # scores：块 id → 各路倒数排名分之和。同一块在越多路命中、名次越靠前，
        # 总分越高——这正是 multi-query 融合的收益来源（改写变体从不同措辞
        # 命中同一批真相关块，RRF 把它们抬到前排）。
        scores: dict[str, float] = {}
        id2chunk: dict[str, Chunk] = {}

        # 向量路：每条 query 一个编码向量，各取 rcfg.vector_topk（编码失败时整路跳过）
        if qvecs is not None:
            for qvec in qvecs:
                for rank, chunk in enumerate(vs.query(qvec, rcfg.vector_topk)):
                    scores[chunk.id] = scores.get(chunk.id, 0.0) + 1.0 / (rrf_k + rank)
                    id2chunk[chunk.id] = chunk

        # BM25 路：每条 query 各查一次 rcfg.bm25_topk；元数据回查合并成一次
        #（不同 query 的命中高度重叠，先收集 union 再一次 fetch_by_ids，避免重复回库）
        bm25_ranked: list[list[str]] = []
        for q in cleaned:
            bm25_ranked.append(
                bm25.query(q, rcfg.bm25_topk) if not bm25.is_empty() else [])
        all_bm25_ids = {i for hits in bm25_ranked for i in hits}
        bm25_chunks = {c.id: c for c in vs.fetch_by_ids(list(all_bm25_ids))}
        for hits in bm25_ranked:
            for rank, doc_id in enumerate(hits):
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (rrf_k + rank)
                if doc_id in bm25_chunks:
                    id2chunk[doc_id] = bm25_chunks[doc_id]

        # 两路名单合起来一个块都没有（索引空/查询无命中）→ 直接返回空
        if not scores:
            return []

        # ---- 候选池与精排（与单 query 版一致，宽召回窄输出）----
        candidates = max(top_k * RERANK_CANDIDATE_MULTIPLIER, rcfg.rerank_candidates)
        ranked_ids = sorted(scores, key=scores.get, reverse=True)[:candidates]
        present = [i for i in ranked_ids if i in id2chunk]
        docs = [id2chunk[i].text for i in present]
        chunks = [id2chunk[i] for i in present]

        # 精排：cross-encoder 用主查询（standalone）打分；失败跳过精排，
        # 按 RRF 初检顺序输出（降级语义）
        reranker = self.service._ensure_reranker()
        try:
            order = reranker(primary, docs, top_k)
        except Exception as e:
            logger.warning("RAG 精排失败，按初检排序输出：%s", e)
            order = list(range(min(top_k, len(chunks))))
        return [chunks[i] for i in order if i < len(chunks)]
