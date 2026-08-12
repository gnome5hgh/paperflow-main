"""检索器：融合 BM25 关键词与向量检索的候选，用 RRF 算法合并排序，再交给重排模型精排。

索引为空时返回空结果。RagRetrieveTool 是暴露给外部调用方的薄封装工具。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service

_RRF_K = 60
_BM25_TOPK = 30
_VECTOR_TOPK = 30


class Retriever:
    """融合检索引擎：同时跑 BM25 关键词检索与向量检索，RRF 合并，再精排。

    与 RagRetrieveTool 是两类职责：这里实现检索与融合算法；RagRetrieveTool
    只做对外暴露的薄封装（取单例、持锁、格式化结果）。
    """

    def __init__(self, service):
        self.service = service

    def retrieve(self, query: str, top_k: int = 5):
        """对 query 执行检索，返回按相关度排序的前 top_k 个块。"""
        # 调用方（RAGService.retrieve / RagRetrieveTool）已持有锁，这里不再加锁。
        # 编码器加载失败会抛出明确异常，不会静默返回空结果。
        embedder = self.service._ensure_embedder()
        qvec = embedder([query])[0]
        vs = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # BM25 索引为空时跳过关键词路；向量检索始终执行。
        bm25_hits = bm25.query(query, _BM25_TOPK) if not bm25.is_empty() else []
        vec_hits = vs.query(qvec, _VECTOR_TOPK)

        # RRF 融合：对每个文档累加它在各候选列表中的 1/(k+排名) 得分，
        # 两个列表都命中的文档得分更高。这里各取 30 个粗候选。
        scores: dict[str, float] = {}
        for rank, doc_id in enumerate(bm25_hits):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)
        for rank, (doc_id, _doc, _dist) in enumerate(vec_hits):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)

        if not scores:
            return []
        # 取融合分最高的 2×top_k 个候选 → 交给重排模型精排 → 按精排顺序还原成 Chunk。
        id2text = {d[0]: d[1] for d in vs.all_documents()}
        ranked_ids = sorted(scores, key=scores.get, reverse=True)[:top_k * 2]
        docs = [id2text.get(i, "") for i in ranked_ids]
        reranker = self.service._ensure_reranker()
        order = reranker(query, docs, top_k)
        # 重排给出的是候选下标，这里按该顺序从向量库取回对应的块。
        chunks = self._chunks_for(ranked_ids)
        return [chunks[i] for i in order if i < len(chunks)]

    def _chunks_for(self, doc_ids: list[str]):
        """按块 id 列表从向量库取回文档，重建轻量 Chunk（含文本、路径、来源）。"""
        from paperflow.rag.parsers.chunker import Chunk
        # 这里不保留切块时的完整信息，只重建查询结果展示所需的字段；
        # 来源按路径后缀判断（.pdf 视为 PDF，其余视为笔记）。
        vs = self.service._ensure_vector_store()
        docs = vs.all_documents()
        by_id = {d[0]: d for d in docs}
        out = []
        for i in doc_ids:
            d = by_id.get(i)
            if d:
                out.append(Chunk(id=i, text=d[1], path=d[2],
                                 source="pdf" if d[2].endswith(".pdf") else "note",
                                 heading="", chunk_index=0))
        return out


class RagRetrieveTool(Tool):
    """对外暴露的检索工具：本身不实现检索逻辑，只惰性获取全局检索服务单例、持锁调用并格式化结果。"""

    name = "rag_retrieve"
    description = ("从本地知识库（笔记 + PDF 全文）检索相关段落。"
                   "参数 query 为检索问题，top_k 为返回块数。")
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "检索问题"},
            "top_k": {"type": "integer", "description": "返回块数", "default": 5},
        },
        "required": ["query"],
    }
    risk_level = "low"

    def __init__(self):
        super().__init__()
        self._service = None

    def execute(self, query: str, top_k: int = 5) -> ToolResult:
        """执行检索并返回格式化结果：每条命中列出来源、路径与文本前 200 字；无命中时给出提示。"""
        svc = self._service or get_rag_service()
        with svc.lock:
            chunks = svc.get_retriever().retrieve(query, top_k)
        if not chunks:
            return ToolResult(text="检索无命中（索引可能为空，可先写几篇笔记）")
        lines = [f"- [{c.source}:{c.path}] {c.text[:200]}" for c in chunks]
        return ToolResult(text="检索到以下相关段落：\n" + "\n".join(lines))
