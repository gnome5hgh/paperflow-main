"""Retriever：BM25 + Vector → RRF → Reranker；空索引回退。RagRetrieveTool 薄包装。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service

_RRF_K = 60
_BM25_TOPK = 30
_VECTOR_TOPK = 30


class Retriever:
    def __init__(self, service):
        self.service = service

    def retrieve(self, query: str, top_k: int = 5):
        # 调用方（RAGService.retrieve / RagRetrieveTool）已持锁
        embedder = self.service._ensure_embedder()   # 加载失败会抛出明确错误（不静默降级）
        qvec = embedder([query])[0]
        vs = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        bm25_hits = bm25.query(query, _BM25_TOPK) if not bm25.is_empty() else []
        vec_hits = vs.query(qvec, _VECTOR_TOPK)

        # RRF 融合：score(d) = Σ 1/(k + rank_i(d))
        scores: dict[str, float] = {}
        for rank, doc_id in enumerate(bm25_hits):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)
        for rank, (doc_id, _doc, _dist) in enumerate(vec_hits):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)

        if not scores:
            return []
        # 取各 id 的文档文本 → rerank → 还原 Chunk
        id2text = {d[0]: d[1] for d in vs.all_documents()}
        ranked_ids = sorted(scores, key=scores.get, reverse=True)[:top_k * 2]
        docs = [id2text.get(i, "") for i in ranked_ids]
        reranker = self.service._ensure_reranker()
        order = reranker(query, docs, top_k)
        # 按重排顺序返回 Chunk（从 vector store 拉全文/路径）
        chunks = self._chunks_for(ranked_ids)
        return [chunks[i] for i in order if i < len(chunks)]

    def _chunks_for(self, doc_ids: list[str]):
        from paperflow.rag.chunker import Chunk
        # 简化：从 vector store 的 documents 重建轻量 Chunk（含 text/path/source）
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
    """RAG 检索薄包装：不做 RAG wiring，惰性取 get_rag_service() 单例。"""

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
        svc = self._service or get_rag_service()
        with svc.lock:
            chunks = svc.get_retriever().retrieve(query, top_k)
        if not chunks:
            return ToolResult(text="检索无命中（索引可能为空，可先写几篇笔记）")
        lines = [f"- [{c.source}:{c.path}] {c.text[:200]}" for c in chunks]
        return ToolResult(text="检索到以下相关段落：\n" + "\n".join(lines))
