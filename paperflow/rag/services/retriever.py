"""检索器：融合 BM25 关键词与向量检索的候选，用 RRF 算法合并排序，再交给重排模型精排。

索引为空时返回空结果。RagRetrieveTool 是暴露给外部调用方的薄封装工具。
"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service


# ---- 混合检索参数配置 ----
# 两路各取 30 个粗候选，保证召回率
# RRF 融合常数 k=60，值越大排名间的分数差异越平滑
# 融合后取 2×top_k 个候选交给精排，防止重排阶段丢失潜在相关结果
_RRF_K = 60
_BM25_TOPK = 30
_VECTOR_TOPK = 30


class Retriever:
    """融合检索引擎：同时跑 BM25 关键词检索与向量检索，RRF 合并，再精排。

    与 RagRetrieveTool 是两类职责：这里实现检索与融合算法；RagRetrieveTool
    只做对外暴露的薄封装（取单例、持锁、格式化结果）。
    """

    def __init__(self, service):
        """绑定门面服务：底层组件（向量库/BM25/编码器/重排器）都经 service 惰性获取。

        Args:
            service: RAGService 单例，底层组件（向量库/BM25/编码器/重排器）都经其惰性获取。
        """
        self.service = service

    def retrieve(self, query: str, top_k: int = 5):
        """对 query 执行检索，返回按相关度排序的前 top_k 个块。

        边界条件：
        - 若 BM25 索引为空，跳过关键词路，只用向量检索。
        - 若两路结果均为空，返回空列表。
        - 若重排后返回的下标超出候选范围，进行安全截断（防御性）。

        Args:
            query: 用户查询文本。
            top_k: 最终需要返回的块数量。

        Returns:
            list[Chunk]: 按相关度降序排列的 Chunk 对象列表，长度不超过 top_k。
        """
        # 调用方（RAGService.retrieve 或 RagRetrieveTool）已持有锁，此处不再加锁
        # 编码器加载失败会抛出明确异常，不静默返回空结果

        # ---- 1. 对查询文本进行向量编码 ----
        embedder = self.service._ensure_embedder()
        # 取第一个（也是唯一一个）向量
        qvec = embedder([query])[0] # 调用 __call__ 方法，传入包含单个字符串的列表。

        vs = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # ---- 2. 双路检索 ----
        # BM25 检索（仅当索引非空）
        bm25_hits = bm25.query(query, _BM25_TOPK) if not bm25.is_empty() else []
        # 向量检索（始终执行，若向量库为空则返回空列表）
        vec_hits = vs.query(qvec, _VECTOR_TOPK) # 返回 [(id, text, distance), ...]

        # ---- 3. RRF 融合两路结果 ----
        # 对每路的每个文档，赋予分数 1/(k + rank)，rank 从 0 开始。同时出现在两路中的文档得分更高，实现互补。
        scores: dict[str, float] = {}
        for rank, doc_id in enumerate(bm25_hits):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)
        for rank, (doc_id, _doc, _dist) in enumerate(vec_hits):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (_RRF_K + rank)
        if not scores:
            return []

        # ---- 4. 筛选候选，准备精排 ----
        # 从向量库获取这些 ID 对应的文本（用于重排模型输入）
        id2text = {d[0]: d[1] for d in vs.all_documents()} # all_documents() 返回 [(id, text, path, mtime), ...]

        # 取融合分最高的 2×top_k 个文档 ID
        ranked_ids = sorted(scores, key=scores.get, reverse=True)[:top_k * 2]
        docs = [id2text.get(i, "") for i in ranked_ids]

        # ---- 5. 精排（Cross-encoder） ----
        # 调用重排模型对这些候选进行精排。
        reranker = self.service._ensure_reranker()
        # 重排模型返回按相关度降序排列的文档下标（对应 docs 列表中的位置）
        order = reranker(query, docs, top_k)

        # ---- 6. 还原为 Chunk 对象 ----
        # 重排给出的是候选下标，这里按该顺序从向量库取回对应的块。
        chunks = self._chunks_for(ranked_ids)
        # 按重排顺序输出，并安全截断（防御 order 可能越界）
        return [chunks[i] for i in order if i < len(chunks)]

    def _chunks_for(self, doc_ids: list[str]):
        """按块 id 列表从向量库取回文档，重建轻量 Chunk（含文本、路径、来源）。

        注意：这里不保留原切块时的完整信息（如 heading、chunk_index），
        只重建查询结果展示所需的字段（id、text、path、source）。

        source 字段由路径后缀推断：.pdf 为 "pdf"，其余为 "note"（包括 .md）。

        Args:
            doc_ids: 块 ID 列表。

        Returns:
            list[Chunk]: 重建的 Chunk 对象列表，顺序与 doc_ids 一致（缺失的 ID 被跳过）。
        """
        from paperflow.rag.parsers.chunker import Chunk

        # 这里不保留切块时的完整信息，只重建查询结果展示所需的字段；
        # 来源按路径后缀判断（.pdf 视为 PDF，其余视为笔记）。
        vs = self.service._ensure_vector_store()
        docs = vs.all_documents()       # all_documents() 返回 [(id, text, path, mtime), ...]
        by_id = {d[0]: d for d in docs} # id -> (id, text, path, mtime)

        out = []
        for i in doc_ids:
            d = by_id.get(i)
            if d:
                # d 为 (id, text, path, mtime)
                out.append(Chunk(id=i,
                                 text=d[1],
                                 path=d[2],
                                 source="pdf" if d[2].endswith(".pdf") else "note",
                                 heading="",   # 检索结果不展示 heading
                                 chunk_index=0 # 检索结果不需要序号
                                 ))
        return out


class RagRetrieveTool(Tool):
    """对外暴露的检索工具：本身不实现检索逻辑，只惰性获取全局检索服务单例、持锁调用并格式化结果。

    职责：
    - 惰性获取全局 RAGService 单例。
    - 持有锁调用检索器。
    - 将检索结果格式化为人类可读的文本，供 Agent 或其他调用方使用。

    与 Retriever 的区别：
    - Retriever 实现核心检索算法。
    - RagRetrieveTool 是工具层封装，负责单例管理、锁控制和输出格式化。
    """

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
    risk_level = "low" # 工具风险等级：只读操作，无风险

    def __init__(self):
        """创建检索工具；_service 延迟到首次 execute 时取全局单例（也支持测试注入）。"""
        super().__init__()
        self._service = None # 可被测试注入，否则在 execute 中取全局单例

    def execute(self, query: str, top_k: int = 5) -> ToolResult:
        """执行检索并返回格式化结果：每条命中列出来源、路径与文本前 200 字；无命中时给出提示。

        Args:
            query: 检索查询。
            top_k: 返回块数。

        Returns:
            ToolResult: 包含格式化文本的 ToolResult 对象。
        """
        # 1. 获取 RAGService 单例（若已注入则使用注入的实例）。
        svc = self._service or get_rag_service()

        # 2. 持锁调用检索器（保证与索引操作的互斥）。
        with svc.lock:
            chunks = svc.get_retriever().retrieve(query, top_k)

        # 3. 若无结果，返回结构化提示信息。
        if not chunks:
            return ToolResult(text="检索无命中（索引可能为空，可先写几篇笔记）")

        # 4. 否则，每条命中格式化为 `- [来源:路径] 文本前200字` 的列表。
        lines = [f"- [{c.source}:{c.path}] {c.text[:200]}" for c in chunks]
        return ToolResult(text="检索到以下相关段落：\n" + "\n".join(lines))
