"""检索器：融合 BM25 关键词与向量检索的候选，用 RRF 算法合并排序，再交给重排模型精排。

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
#: 只加 query 侧、文档侧不加，官方称可提升 1–5%）。文档编码在索引器完成，
#: 不受影响；意图路由复用同一 embedder，也走各自调用、无此前缀。
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

    def retrieve(self, query: str, top_k: int = 5,
                 source: str | None = None) -> list[Chunk]:
        """对 query 执行检索，返回按相关度排序的前 top_k 个块。

        Args:
            source: 限定来源——"note" 只搜笔记，"pdf" 只搜论文；None 不过滤。
                    非法值按 None 处理（防御性）。

        并发约定：调用方（RAGService.retrieve 或 RagRetrieveTool）已持有锁，
        此处不再加锁——锁由工具层/门面层统一控制，若在此重复加锁会造成
        死锁或锁语义混乱（防止后人「顺手补锁」的关键 WHY）。

        边界条件：
        - BM25 索引为空 → 只用向量检索；两路均为空 → 返回空列表。
        - 重排返回的下标越界 → 安全截断（防御性）。
        """
        if source not in _VALID_SOURCES:
            source = None

        embedder = self.service._ensure_embedder()
        # 仅 query 侧加指令前缀（Qwen3 官方格式）
        qvec = embedder([f"Instruct: {_QUERY_INSTRUCTION}\nQuery: {query}"])[0]

        vs = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # ---- 双路检索（向量路结果自带 text/path/source，不再全表回查）----
        expr = f'source == "{source}"' if source else ""
        vec_hits = vs.query(qvec, _VECTOR_TOPK, expr=expr)   # (id, text, path, source, dist)
        bm25_hits = bm25.query(query, _BM25_TOPK) if not bm25.is_empty() else []
        bm25_docs = {d[0]: d for d in vs.fetch_by_ids(bm25_hits)}   # id -> (id, text, path, source)
        if source:
            # BM25 路无原生过滤，取回元数据后按 source 筛（候选变少可接受，
            # 个人语料规模下不为此放大 top）
            bm25_hits = [i for i in bm25_hits
                         if i in bm25_docs and bm25_docs[i][3] == source]

        # ---- RRF 融合两路结果 ----
        scores: dict[str, float] = {}
        for rank, doc_id in enumerate(bm25_hits):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)
        for rank, hit in enumerate(vec_hits):
            scores[hit[0]] = scores.get(hit[0], 0.0) + 1.0 / (_RRF_K + rank)
        if not scores:
            return []

        # ---- 候选池与精排 ----
        # 候选池 = max(2×top_k, 配置值)：RRF 分数会把「只在单路靠前」的真命中
        # 压到中游，池子太小会在精排前就把它们截掉（BAAI 惯例：宽召回窄输出）
        candidates = max(top_k * 2,
                         int(getattr(self.service.config, "rag_rerank_candidates", 24)))
        ranked_ids = sorted(scores, key=scores.get, reverse=True)[:candidates]

        id2doc: dict[str, tuple] = {hit[0]: hit for hit in vec_hits}
        id2doc.update(bm25_docs)
        present = [i for i in ranked_ids if i in id2doc]
        docs = [id2doc[i][1] for i in present]
        # 检索端不做前缀行解析（前缀行与正文首行无可靠区分标记）：
        # heading 恒空，标题上下文由块文本首行自然携带、随摘录展示
        chunks = [Chunk(id=i, text=id2doc[i][1], path=id2doc[i][2],
                        source=id2doc[i][3], heading="", chunk_index=0)
                  for i in present]

        reranker = self.service._ensure_reranker()
        order = reranker(query, docs, top_k)
        return [chunks[i] for i in order if i < len(chunks)]
