"""检索器：融合 BM25 关键词与向量检索的候选（支持多查询改写集，同一 RRF 池融合），再交给重排模型精排。

索引为空时返回空结果。对外检索工具 RagRetrieveTool 在 `paperflow/tools/rag/rag_retrieve.py`。
"""
from paperflow.rag.parsers.chunker import Chunk


# ---- 混合检索参数配置 ----
# 两路各取 30 个粗候选，保证召回率
# RRF 融合常数 k=60，值越大排名间的分数差异越平滑
_RRF_K = 60
_BM25_TOPK = 30
_VECTOR_TOPK = 30

#: query 侧任务指令（Qwen3-Embedding 官方格式 Instruct: {task}\nQuery: {query}，
#: 只加 query 侧、文档侧不加，官方称可提升 1–5%）。
# 文档编码在索引器完成，不受影响；意图路由复用同一 embedder，也走各自调用、无此前缀。
_QUERY_INSTRUCTION = ("Given an academic research query, retrieve relevant "
                      "passages from papers and reading notes")

#: source 过滤的合法取值；超出按不过滤处理（工具层已有 enum 约束，此处防御）
_VALID_SOURCES = (None, "note", "pdf")


class Retriever:
    """融合检索引擎：同时跑 BM25 关键词检索与向量检索，RRF 合并，再精排。

    与检索工具 RagRetrieveTool（tools/rag）是两类职责：这里实现检索与融合算法；
    RagRetrieveTool 只做对外暴露的薄封装（取单例、持锁、格式化结果）。
    """

    def __init__(self, service):
        """绑定门面服务：底层组件（向量库/BM25/编码器/重排器）都经 service 惰性获取。"""
        self.service = service
        # BM25 进程级同步标记：BM25 是内存投影，进程重启即空，首次查询前必须
        # 从向量库整体重建一次才能与向量路对齐；此后增删改由索引器与向量库
        # 成对执行维护，无需再重建。
        self._bm25_synced = False

    def retrieve(self, queries, top_k: int = 5,
                 source: str | None = None) -> list[Chunk]:
        """对查询集执行检索：每条 query 独立跑双路，全部排名进同一 RRF 池融合。

        Args:
            queries: 查询集（str 视为单条）——queries[0] 为主查询（reranker
                     用它打分）；其余为改写变体，词面不同、语义等价。
            source: 限定来源——"note" 只搜笔记，"pdf" 只搜论文；None 不过滤。
                    非法值按 None 处理（防御性）。

        并发约定：调用方（RagRetrieveTool）已持有锁，此处不再加锁——锁由
        工具层统一控制，若在此重复加锁会造成死锁或锁语义混乱（防止后人
        「顺手补锁」的关键 WHY）。

        边界条件：
        - BM25 索引为空 → 只用向量检索；两路均为空 → 返回空列表。
        - 空查询集/全空串 → 归一为 [""]（与空索引组合时返回空列表，不炸）。
        - 重排返回的下标越界 → 安全截断（防御性）。
        """
        if isinstance(queries, str):
            queries = [queries]
        # 查询集归一：去空串；全空则占位一条，让下游按「查无此人」自然返回空
        cleaned = [q.strip() for q in queries if q and q.strip()] or [""]
        primary = cleaned[0]

        if source not in _VALID_SOURCES:
            source = None

        embedder = self.service._ensure_embedder()
        # 每条 query 各拼指令前缀（Qwen3 官方格式），一次批量编码
        qvecs = embedder([f"Instruct: {_QUERY_INSTRUCTION}\nQuery: {q}"
                          for q in cleaned])

        vs = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # ---- BM25 进程级恢复（裸 rebuild）----
        # 每进程同步一次，而非「空了才补」：重启后若先发生一次写热更新，
        # BM25 只含新写的一篇，与向量库已漂移（非空但残缺），is_empty 探测不到。
        # 重建以向量库原文为唯一源，只恢复 BM25 内存索引，
        # 不触碰索引状态与文档块（那是 index_all 全量重扫的职责，个人语料规模下两者开销差一个量级）。
        # 重建失败（Milvus 读取异常）不置位，下次查询重试；本次退化为纯向量路。
        if not self._bm25_synced:
            bm25.rebuild([(d[0], d[1]) for d in vs.all_documents()])
            self._bm25_synced = True

        expr = f'source == "{source}"' if source else ""

        # ---- 多查询双路检索，全部排名累计进同一 RRF 池 ----
        # scores：块 id → 各路倒数排名分之和。同一块在越多路命中、名次越靠前，
        # 总分越高——这正是 multi-query 融合的收益来源（改写变体从不同措辞
        # 命中同一批真相关块，RRF 把它们抬到前排）。
        scores: dict[str, float] = {}
        id2doc: dict[str, tuple] = {}

        # 向量路：每条 query 一个编码向量，各取 top30
        for qvec in qvecs:
            for rank, hit in enumerate(vs.query(qvec, _VECTOR_TOPK, expr=expr)):
                scores[hit[0]] = scores.get(hit[0], 0.0) + 1.0 / (_RRF_K + rank)
                id2doc[hit[0]] = hit

        # BM25 路：每条 query 各查一次 top30；档案回查合并成一次
        #（不同 query 的命中高度重叠，先收集 union 再一次 fetch_by_ids，避免重复回库）
        bm25_ranked: list[list[str]] = []
        for q in cleaned:
            hits = bm25.query(q, _BM25_TOPK) if not bm25.is_empty() else []
            if source:
                # BM25 路无原生过滤，取回元数据后按 source 筛
                docs = {d[0]: d for d in vs.fetch_by_ids(hits)}
                hits = [i for i in hits if i in docs and docs[i][3] == source]
            bm25_ranked.append(hits)
        all_bm25_ids = {i for hits in bm25_ranked for i in hits}
        bm25_docs = {d[0]: d for d in vs.fetch_by_ids(list(all_bm25_ids))}
        for hits in bm25_ranked:
            for rank, doc_id in enumerate(hits):
                scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)
                if doc_id in bm25_docs:
                    id2doc[doc_id] = bm25_docs[doc_id]

        # 两路名单合起来一个块都没有（索引空/查询无命中）→ 直接返回空
        if not scores:
            return []

        # ---- 候选池与精排（与单 query 版一致，宽召回窄输出）----
        candidates = max(top_k * 2,
                         int(getattr(self.service.config, "rag_rerank_candidates", 24)))
        ranked_ids = sorted(scores, key=scores.get, reverse=True)[:candidates]
        present = [i for i in ranked_ids if i in id2doc]
        docs = [id2doc[i][1] for i in present]
        chunks = [Chunk(id=i, text=id2doc[i][1], path=id2doc[i][2],
                        source=id2doc[i][3], heading="", chunk_index=0)
                  for i in present]

        # 精排：cross-encoder 用主查询（standalone，指代消解后最完整的表述）打分
        reranker = self.service._ensure_reranker()
        order = reranker(primary, docs, top_k)
        return [chunks[i] for i in order if i < len(chunks)]
