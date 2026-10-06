"""RAGService：整个检索栈的单一实例（索引与检索必须共享同一实例才能增量更新）。

索引器与检索器是同一个实例的两个视图。如果各自创建一套组件，写入时
更新的是实例 A 的索引，查询时用的却是实例 B 里陈旧的 BM25，导致新增
内容检索不到。整个检索与索引过程共用同一把可重入锁（含模型推理），
整段串行执行是最简单且正确的并发模型。
"""
import threading

from paperflow.config import PaperFlowConfig
from paperflow.rag.parsers.chunker import AcademicChunker
from paperflow.rag.parsers.grobid_client import ParsedDoc


class RAGService:
    """检索服务的外观：统一持有向量库、BM25、编码器、重排器、解析器等组件，全部惰性加载。

    每个组件经 _ensure_* 方法用「双重检查加锁」构造：先看缓存，为空才进锁
    再看一次，仍空才真正构造——保证并发下只初始化一次（模型加载耗时数秒，
    重复加载既慢又浪费）。索引器与检索器必须共享同一实例，见模块注释。
    """

    def __init__(self, config: PaperFlowConfig):
        """初始化门面：先把所有组件槽位置空，首次访问才惰性构造。

        除 chunker（纯逻辑、构造无副作用）外，编码器/重排器/向量库/BM25/
        GROBID 都走懒加载——既避免启动时加载数秒的模型，也让不碰检索的
        测试完全不会导入重依赖。

        Args:
            config: 全局配置对象，包含工作区路径、模型名、GROBID端点等。
        """
        self.config = config
        # 可重入锁，保护整个索引/检索流程，确保并发下状态一致
        self.lock = threading.RLock()

        # ---- 惰性加载的组件槽位 ----
        self._embedder = None          # 稠密向量编码器 (CloudEmbedder)
        self._reranker = None          # 精排模型 (CloudReranker)
        self._grobid = None            # GROBID 客户端 (GrobidClient)
        self._pymupdf_parser = None    # PyMuPDF 备用解析器
        self._grobid_available = None  # 缓存 GROBID 可用性探测结果 (bool | None)
        self._vector_store = None      # 向量库 (VectorStore)
        self._milvus_available = None    # Milvus 可连性探测缓存 (bool | None)
        self._bm25 = None              # BM25 索引 (Bm25Index)
        self._indexer = None           # 索引器视图 (RagIndexer)
        self._retriever = None         # 检索器视图 (Retriever)
        self._rewriter = None          # 改写器 (QueryRewriter)

        # 纯逻辑组件，无副作用，直接构造。切块参数读配置（rag.chunker.*），
        # 配方哈希据此失效——改 YAML 即触发全量重索引。
        self.chunker = AcademicChunker(config.rag.chunker.max_tokens,
                                       config.rag.chunker.overlap_tokens)

        # GROBID 解析结果缓存：键为 (绝对路径, 修改时间戳, 文件大小)，
        # 值是对应的 ParsedDoc。进程内缓存避免同一 PDF 被反复解析。
        # 当 PDF 被替换时，修改时间或文件大小变化，键自动失效。
        # 使用实例属性而非类属性，确保不同测试实例独立。
        self._parse_cache: dict[tuple[str, int, int], "ParsedDoc"] = {}

    # —— 惰性组件（双重检查加锁，保证并发下只初始化一次）——
    def _ensure_embedder(self):
        """惰性获取编码器：首次访问时构造并缓存。

        Returns:
            CloudEmbedder: 云端编码器实例（构造不碰网络，失败在调用时暴露）。
        """
        # 双重检查加锁：先检查实例变量是否为空，为空则获取锁后再次检查，
        # 确保并发下只有一个线程执行构造，其余线程复用已构造的实例。
        if self._embedder is None:
            with self.lock:
                if self._embedder is None:
                    from paperflow.core.llm.embedding import CloudEmbedder
                    emb = self.config.rag.embedding
                    self._embedder = CloudEmbedder(emb.base_url, emb.api_key,
                                                   emb.embed_model,
                                                   batch_size=emb.batch_size,
                                                   timeout=emb.timeout,
                                                   max_retries=emb.max_retries)
        return self._embedder

    def _ensure_reranker(self):
        """惰性获取重排模型：首次访问时构造并缓存。

        Returns:
            CloudReranker: 云端重排器实例（构造不碰网络，失败在调用时暴露）。
        """
        if self._reranker is None:
            with self.lock:
                if self._reranker is None:
                    from paperflow.core.llm.rerank import CloudReranker
                    # 直接构造 config 的调用方（测试/嵌入宿主）未必经过 from_env 的继承回填，
                    # 故此处对空的端点/key 再兜底继承 embedding 一次。
                    rr = self.config.rag.rerank
                    emb = self.config.rag.embedding
                    self._reranker = CloudReranker(rr.base_url or emb.base_url,
                                                   rr.api_key or emb.api_key,
                                                   rr.model,
                                                   timeout=rr.timeout,
                                                   max_retries=rr.max_retries)
        return self._reranker

    def _ensure_vector_store(self):
        """惰性获取向量库：首次访问时打开（必要时创建）。

        维度从 embedder 读取（建集合时定死）；uri 来自配置（本地文件→Lite、
        http→Standalone）。连接失败抛带可行动指引的错——Milvus 是服务而非
        本地文件，可能未启动，不能假设向量库永不宕机。

        Returns:
            VectorStore: 向量库实例。

        Raises:
            RuntimeError: Milvus 未连接（附 `docker compose up -d` 指引）。
        """
        if self._vector_store is None:
            with self.lock:
                if self._vector_store is None:
                    from paperflow.rag.storage.vector_store import VectorStore
                    dim = self._ensure_embedder().dim
                    try:
                        self._vector_store = VectorStore(
                            self.config.rag.storage.uri, dim,
                            collection_name=self.config.rag.storage.collection,
                            batch_size=self.config.rag.storage.batch_size,
                        )
                    except Exception as e:
                        raise RuntimeError(
                            f"Milvus 未连接（{self.config.rag.storage.uri}）：{e}。"
                            "请运行 `docker compose up -d` 启动服务后重试。"
                        ) from e
        return self._vector_store

    def milvus_available(self) -> bool:
        """探测 Milvus 是否可连接（仅作启动期探测，结果不中途刷新）。

        Lite（本地文件 uri）恒可连；Standalone 未启动则 False。结果缓存在
        进程生命周期内——中途容器崩溃不由本方法感知，运行期可观测性由
        rag_retrieve 工具的固定降级声明保证。

        Returns:
            bool: True 表示可连接。
        """
        if self._milvus_available is None:
            try:
                self._ensure_vector_store()
                self._milvus_available = True
            except RuntimeError:
                self._milvus_available = False
        return self._milvus_available

    def _ensure_bm25(self):
        """惰性获取 BM25 索引：首次访问时创建。

        Returns:
            Bm25Index: BM25 索引实例。
        """
        # Bm25Index 是纯内存结构，无持久化，进程重启后需从向量库重建。
        # 这里只创建空索引；填充有两条路径——检索器首次查询时的进程级恢复
        # （retriever.py，裸 rebuild 自向量库）与索引器的增量/全量写入。
        if self._bm25 is None:
            with self.lock:
                if self._bm25 is None:
                    from paperflow.rag.encoders.bm25 import Bm25Index
                    self._bm25 = Bm25Index()
        return self._bm25

    # ---------- PDF 解析与 GROBID 相关 ----------
    def grobid_available(self) -> bool:
        """探测 GROBID 服务是否可用，结果在本次会话内缓存（不会中途变卦）。

        Returns:
            bool: True 表示服务可用，False 表示不可用。
        """
        if self._grobid_available is None:
            with self.lock:
                if self._grobid_available is None:
                    from paperflow.rag.parsers.grobid_client import GrobidClient
                    gb = self.config.rag.grobid
                    self._grobid = GrobidClient(gb.endpoint, timeout=gb.timeout)
                    self._grobid_available = self._grobid.available()
        return self._grobid_available

    def pdf_parser(self):
        """返回 PDF 解析器：GROBID 可用时用它的客户端，否则改用 PyMuPDF 启发式解析器。

        选择策略：
        - GROBID 可用 → 使用 GrobidClient（提供结构化章节、表格、图片说明）
        - GROBID 不可用 → 使用 PyMuPDFParser（按字号粗略切分章节，精度够分块使用）

        Returns:
            GrobidClient 或 PyMuPDFParser 实例。
        """
        if self.grobid_available():
            return self._grobid
        if self._pymupdf_parser is None:
            from paperflow.rag.parsers.grobid_client import PyMuPDFParser
            self._pymupdf_parser = PyMuPDFParser()
        return self._pymupdf_parser

    def parse_pdf_cached(self, path: str) -> "ParsedDoc":
        """解析 PDF 并做进程内缓存（只加速，不改变结果）。

        缓存键由 (绝对路径, 修改时间戳, 文件大小) 组成：
        - 同一路径的文件被替换时，修改时间和大小会变，缓存自动失效。
        - 解析失败时结果不入缓存，下次调用会重新尝试解析（异常上抛）。

        Args:
            path: PDF 文件的路径（可以是相对或绝对）。

        Returns:
            ParsedDoc: 解析得到的结构化文档对象。

        Raises:
            可能抛出 httpx.HTTPError, ET.ParseError 等，由上层处理。
        """
        from pathlib import Path
        resolved = Path(path).resolve()
        st = resolved.stat()
        key = (str(resolved), st.st_mtime_ns, st.st_size)

        with self.lock:
            # 先检查缓存
            hit = self._parse_cache.get(key)
            if hit is not None:
                return hit

            # 缓存未命中，持锁解析。此处虽然持锁，但索引/检索流程本就串行，
            # 不会引入额外的并发问题；若未来改为并发解析，需考虑重复解析问题。
            doc = self.pdf_parser().parse_pdf(str(resolved))
            # 只有成功解析的结果才放入缓存
            self._parse_cache[key] = doc
            return doc

    # ---------- 索引器/检索器视图（延迟创建） ----------
    def get_indexer(self):
        """惰性创建并返回索引器视图。

        索引器 indexer 和检索器 retriever 共享同一个 RAGService 实例，因此共享所有底层组件。

        Returns:
            RagIndexer: 索引器实例。
        """
        if self._indexer is None:
            # 使用函数内导入避免模块循环依赖
            from paperflow.rag.services.indexer import RagIndexer
            self._indexer = RagIndexer(self)
        return self._indexer

    def get_retriever(self):
        """惰性创建并返回检索器视图。

        Returns:
            Retriever: 检索器实例。
        """
        if self._retriever is None:
            from paperflow.rag.services.retriever import Retriever
            self._retriever = Retriever(self)
        return self._retriever

    def get_rewriter(self):
        """惰性创建并返回 query 改写器（RAG 包内首个 LLM 调用点）。

        连接参数取 config.rag.query_rewrite 三元组，以主 LLM 为基底逐项覆盖：
        base_url/api_key 留空（from_env 已继承主 LLM，此处再兜底）沿用主配置，
        model 留空沿用主模型（历史默认行为）。LLMClient 对空 api_key fail-fast
        ——调用方（RagRetrieveTool）catch 后降级原 query。

        Returns:
            QueryRewriter: 改写器实例（进程内缓存）。
        """
        if self._rewriter is None:
            from dataclasses import replace

            from paperflow.core.llm.client import LLMClient
            from paperflow.rag.services.query_rewriter import QueryRewriter
            # 以主 LLM 配置为基底，query_rewrite 三元组逐项覆盖（空值回退主配置）。
            # 直接构造 config 的调用方（测试/嵌入宿主）未必经过 from_env 的继承回填，
            # 故此处对空值再兜底一次。
            qr = self.config.rag.query_rewrite
            llm_cfg = replace(
                self.config.llm,
                base_url=qr.base_url or self.config.llm.base_url,
                api_key=qr.api_key or self.config.llm.api_key,
                model=qr.model or self.config.llm.model,
            )
            self._rewriter = QueryRewriter(
                LLMClient(llm_cfg),
                history_limit=self.config.rag.query_rewrite.history_messages,
                rewrite_num=self.config.rag.query_rewrite.rewrite_num,
                max_queries=self.config.rag.query_rewrite.max_queries,
                max_query_chars=self.config.rag.query_rewrite.max_query_chars)
        return self._rewriter

    # ---------- 对外便捷入口（索引/检索持同一把锁） ----------
    def index_document(self, path: str) -> None:
        """单篇文档的增量重索引（持锁）。

        调用索引器的 index_document 方法，在锁保护下执行，保证索引状态一致。

        Args:
            path: 文档的绝对路径。
        """
        with self.lock:
            self.get_indexer().index_document(path)

    def index_all(self) -> None:
        """全量增量扫描：重索引新增/变更文档、清理已删除文档（持锁）。

        这里必须与单篇索引一样持同一把锁：索引过程会重建 BM25、整库写入
        向量库，若不持锁，查询会读到写到一半的中间状态。
        """
        with self.lock:
            self.get_indexer().index_all()

    def retrieve(self, query: str, top_k: int | None = None):
        """检索入口（持锁），返回按相关度排序的块列表。

        Args:
            query: 检索查询文本。
            top_k: 需要返回的结果块数；None 时取配置 ``rag.retriever.top_k``
                （默认值单点在 config，不再用模块常量字面量）。

        Returns:
            list[Chunk]: 按相关度降序排列的 Chunk 对象列表。
        """
        if top_k is None:
            top_k = self.config.rag.retriever.top_k
        with self.lock:
            return self.get_retriever().retrieve(query, top_k)


# ---------- 模块级单例 ----------
_rag_service: RAGService | None = None
_rag_singleton_lock = threading.RLock()


def get_rag_service(config: PaperFlowConfig | None = None) -> RAGService:
    """模块级单例：所有调用方共享同一个检索服务实例（增量更新的前提）。

    双重检查加锁，保证并发下只创建一次。
    如果未传入 config，则从环境变量加载默认配置。

    Args:
        config: 可选配置对象，不传则使用 PaperFlowConfig.from_env()。

    Returns:
        RAGService: 全局唯一的检索服务实例。
    """
    global _rag_service
    if _rag_service is None:
        with _rag_singleton_lock:
            if _rag_service is None:
                _rag_service = RAGService(config or PaperFlowConfig.from_env())
    return _rag_service
