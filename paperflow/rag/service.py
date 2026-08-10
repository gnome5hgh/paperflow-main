"""RAGService：整个检索栈的单一实例（索引与检索必须共享同一实例才能增量更新）。

索引器与检索器是同一个实例的两个视图。如果各自创建一套组件，写入时
更新的是实例 A 的索引，查询时用的却是实例 B 里陈旧的 BM25，导致新增
内容检索不到。整个检索与索引过程共用同一把可重入锁（含模型推理），
整段串行执行是最简单且正确的并发模型。
"""
import threading

from paperflow.config import PaperFlowConfig
from paperflow.rag.chunker import AcademicChunker


class RAGService:
    """检索服务的外观：统一持有向量库、BM25、编码器、重排器、解析器等组件，全部惰性加载。"""

    def __init__(self, config: PaperFlowConfig):
        self.config = config
        self.lock = threading.RLock()
        # 惰性组件：首次访问才构造（模型加载耗时数秒，也避免测试互相污染）
        self._embedder = None
        self._reranker = None
        self._grobid = None
        self._pymupdf_parser = None
        self._grobid_available = None       # 缓存探测结果（本次会话内不变）
        self._vector_store = None
        self._bm25 = None
        self._indexer = None
        self._retriever = None
        self.chunker = AcademicChunker()    # 纯逻辑组件，构造无副作用
        # GROBID 解析结果缓存：key = (绝对路径, 修改时间戳, 文件大小)。
        # 背景：多轮审稿时同一 PDF 会被反复解析，进程内缓存让同一单例下的
        # 所有调用共享一份解析结果，把多次解析降为一次。PDF 被替换时
        # 修改时间和文件大小都会变化，缓存键随之失效，无需手动维护。
        # 用实例属性而非类属性：每个测试实例相互独立，避免跨测试互相污染。
        self._parse_cache: dict[tuple[str, int, int], "ParsedDoc"] = {}

    # —— 惰性组件（双重检查加锁，保证并发下只初始化一次）——
    def _ensure_embedder(self):
        """惰性获取编码器：首次访问时构造并缓存。"""
        if self._embedder is None:
            with self.lock:
                if self._embedder is None:
                    from paperflow.rag.embedder import BgeEmbedder, resolve_model_dir
                    # 模型路径本地优先（工作区 models 目录），否则改用官方模型名
                    self._embedder = BgeEmbedder(resolve_model_dir(
                        self.config.workspace, self.config.embed_model))
        return self._embedder

    def _ensure_reranker(self):
        """惰性获取重排模型：首次访问时构造并缓存。"""
        if self._reranker is None:
            with self.lock:
                if self._reranker is None:
                    from paperflow.rag.reranker import BgeReranker
                    from paperflow.rag.embedder import resolve_model_dir
                    # 模型路径本地优先（工作区 models 目录），否则改用官方模型名
                    self._reranker = BgeReranker(resolve_model_dir(
                        self.config.workspace, self.config.rerank_model))
        return self._reranker

    def _ensure_vector_store(self):
        """惰性获取向量库：首次访问时打开（必要时创建）。"""
        if self._vector_store is None:
            with self.lock:
                if self._vector_store is None:
                    from paperflow.rag.vector_store import VectorStore
                    self._vector_store = VectorStore(self.config.chroma_dir)
        return self._vector_store

    def _ensure_bm25(self):
        """惰性获取 BM25 索引：首次访问时创建。"""
        if self._bm25 is None:
            with self.lock:
                if self._bm25 is None:
                    from paperflow.rag.bm25_index import Bm25Index
                    self._bm25 = Bm25Index()
        return self._bm25

    def grobid_available(self) -> bool:
        """探测 GROBID 服务是否可用，结果在本次会话内缓存（不会中途变卦）。"""
        if self._grobid_available is None:
            with self.lock:
                if self._grobid_available is None:
                    from paperflow.rag.grobid_client import GrobidClient
                    self._grobid = GrobidClient(self.config.grobid_url)
                    self._grobid_available = self._grobid.available()
        return self._grobid_available

    def pdf_parser(self):
        """返回 PDF 解析器：GROBID 可用时用它的客户端，否则改用 PyMuPDF 启发式解析器。"""
        if self.grobid_available():
            return self._grobid
        if self._pymupdf_parser is None:
            from paperflow.rag.grobid_client import PyMuPDFParser
            self._pymupdf_parser = PyMuPDFParser()
        return self._pymupdf_parser

    def parse_pdf_cached(self, path: str) -> "ParsedDoc":
        """解析 PDF 并做进程内缓存（只加速，不改变结果）。

        缓存键是 (绝对路径, 修改时间, 文件大小)：同一路径的 PDF 被替换时
        键会变化，缓存自动失效重新解析。解析失败时结果不入缓存——
        异常不固化，下次调用会重试。
        """
        from pathlib import Path
        resolved = Path(path).resolve()
        st = resolved.stat()
        key = (str(resolved), st.st_mtime_ns, st.st_size)
        with self.lock:
            hit = self._parse_cache.get(key)
            if hit is not None:
                return hit
            # 持锁解析：并发情况下只有第一个未命中者真正解析，其余等待复用。
            # 检索与索引过程本就整段持锁串行，这里不引入额外的并发。
            doc = self.pdf_parser().parse_pdf(str(resolved))
            self._parse_cache[key] = doc
            return doc

    # —— 视图（延迟创建；用函数内导入避免模块之间的循环依赖）——
    def get_indexer(self):
        """惰性创建并返回索引器视图。"""
        if self._indexer is None:
            from paperflow.rag.indexer import RagIndexer
            self._indexer = RagIndexer(self)
        return self._indexer

    def get_retriever(self):
        """惰性创建并返回检索器视图。"""
        if self._retriever is None:
            from paperflow.rag.retriever import Retriever
            self._retriever = Retriever(self)
        return self._retriever

    # —— 便捷入口：索引/检索都持同一把锁 ——
    def index_document(self, path: str) -> None:
        """单篇文档的增量重索引（持锁）。"""
        with self.lock:
            self.get_indexer().index_document(path)

    def index_all(self) -> None:
        """全量增量扫描：重索引新增/变更文档、清理已删除文档（持锁）。

        这里必须与单篇索引一样持同一把锁：索引过程会重建 BM25、整库写入
        向量库，若不持锁，查询会读到写到一半的中间状态。
        """
        with self.lock:
            self.get_indexer().index_all()

    def retrieve(self, query: str, top_k: int = 5):
        """检索入口（持锁），返回按相关度排序的块列表。"""
        with self.lock:
            return self.get_retriever().retrieve(query, top_k)


_rag_service: RAGService | None = None
_rag_singleton_lock = threading.RLock()


def get_rag_service(config: PaperFlowConfig | None = None) -> RAGService:
    """模块级单例：所有调用方共享同一个检索服务实例（增量更新的前提）。

    双重检查加锁，保证并发下只创建一次。
    """
    global _rag_service
    if _rag_service is None:
        with _rag_singleton_lock:
            if _rag_service is None:
                _rag_service = RAGService(config or PaperFlowConfig.from_env())
    return _rag_service
