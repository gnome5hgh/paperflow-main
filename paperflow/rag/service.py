"""RAGService：单一 RAG 栈单例（热更新成立的前提）。

indexer 与 retriever 是同一实例的视图——若各建各的栈，Write→index_document
更新栈 A、query→retrieve 用栈 B 的陈旧 BM25，热更新静默失效。
整段 retrieve()/index_document() 持同一把 RLock（含模型推理），整段串行最简单正确。
"""
import threading

from paperflow.config import PaperFlowConfig
from paperflow.rag.chunker import AcademicChunker


class RAGService:
    def __init__(self, config: PaperFlowConfig):
        self.config = config
        self.lock = threading.RLock()
        # 惰性组件：首次访问才构造（模型加载耗时数秒，且避免测试污染）
        self._embedder = None
        self._reranker = None
        self._grobid = None
        self._pymupdf_parser = None
        self._grobid_available = None       # 缓存探测结果（当次会话不回退）
        self._vector_store = None
        self._bm25 = None
        self._indexer = None
        self._retriever = None
        self.chunker = AcademicChunker()    # 纯逻辑，构造无副作用
        # GROBID 解析缓存：key = (绝对路径, mtime_ns, size)。
        # 【实测瓶颈修复】reviewer 每轮审稿都 read_pdf → 3 轮审稿 4 次解析；
        # 进程内存缓存让 writer 与 reviewer（同一 RAGService 单例）
        # 共享一份全文，4×→1×。PDF 替换时 mtime+size 变化自动失效，零维护。
        # 实例属性而非类属性：测试每例独立实例，避免跨测试污染。
        self._parse_cache: dict[tuple[str, int, int], "ParsedDoc"] = {}

    # —— 惰性组件（double-checked locking）——
    def _ensure_embedder(self):
        if self._embedder is None:
            with self.lock:
                if self._embedder is None:
                    from paperflow.rag.embedder import BgeEmbedder, resolve_model_dir
                    # 模型路径本地优先（data/models/<name>），回退 HF 名（resolve_model_dir）
                    self._embedder = BgeEmbedder(resolve_model_dir(
                        self.config.workspace, self.config.embed_model))
        return self._embedder

    def _ensure_reranker(self):
        if self._reranker is None:
            with self.lock:
                if self._reranker is None:
                    from paperflow.rag.reranker import BgeReranker
                    self._reranker = BgeReranker(self.config.rerank_model)
        return self._reranker

    def _ensure_vector_store(self):
        if self._vector_store is None:
            with self.lock:
                if self._vector_store is None:
                    from paperflow.rag.vector_store import VectorStore
                    self._vector_store = VectorStore(self.config.chroma_dir)
        return self._vector_store

    def _ensure_bm25(self):
        if self._bm25 is None:
            with self.lock:
                if self._bm25 is None:
                    from paperflow.rag.bm25_index import Bm25Index
                    self._bm25 = Bm25Index()
        return self._bm25

    def grobid_available(self) -> bool:
        """首次使用 RAG 栈时探测并缓存（当次会话不回退），与惰性实例化时机一致。"""
        if self._grobid_available is None:
            with self.lock:
                if self._grobid_available is None:
                    from paperflow.rag.grobid_client import GrobidClient
                    self._grobid = GrobidClient(self.config.grobid_url)
                    self._grobid_available = self._grobid.available()
        return self._grobid_available

    def pdf_parser(self):
        """GROBID 可用返回 GrobidClient，否则 PyMuPDF 回退。"""
        if self.grobid_available():
            return self._grobid
        if self._pymupdf_parser is None:
            from paperflow.rag.grobid_client import PyMuPDFParser
            self._pymupdf_parser = PyMuPDFParser()
        return self._pymupdf_parser

    def parse_pdf_cached(self, path: str) -> "ParsedDoc":
        """GROBID 解析 + 进程内存缓存（透明加速，语义不变）。

        key = (绝对路径, mtime_ns, size)：同一路径 PDF 被替换时自动失效重解析。
        GROBID 抛异常不缓存——故障不固化，下次调用重试（D4）。
        """
        from pathlib import Path
        resolved = Path(path).resolve()
        st = resolved.stat()
        key = (str(resolved), st.st_mtime_ns, st.st_size)
        with self.lock:
            hit = self._parse_cache.get(key)
            if hit is not None:
                return hit
            # 持锁解析：并发首个 miss 只解析一次。RAG 段本就整段持锁串行
            #（GROBID 是单 Docker 服务，fulltext 请求本身就串行处理），
            # 不引入额外串行化——与 index/retrieve 持锁语义一致。
            doc = self.pdf_parser().parse_pdf(str(resolved))
            self._parse_cache[key] = doc
            return doc

    # —— 视图（懒导入防循环 import）——
    def get_indexer(self):
        if self._indexer is None:
            from paperflow.rag.indexer import RagIndexer
            self._indexer = RagIndexer(self)
        return self._indexer

    def get_retriever(self):
        if self._retriever is None:
            from paperflow.rag.retriever import Retriever
            self._retriever = Retriever(self)
        return self._retriever

    # —— 便捷入口：索引/检索持有锁——
    def index_document(self, path: str) -> None:
        with self.lock:
            self.get_indexer().index_document(path)

    def index_all(self) -> None:
        # IMPORTANT-2 修复：index_all 全量扫描此前只能走 get_indexer().index_all()
        # 绕过锁，与持锁的 index_document/retrieve 不一致——并发下 BM25 重建、
        # ChromaDB 全量写会与查询读半截状态。与 index_document 同级暴露持锁入口。
        with self.lock:
            self.get_indexer().index_all()

    def retrieve(self, query: str, top_k: int = 5):
        with self.lock:
            return self.get_retriever().retrieve(query, top_k)


_rag_service: RAGService | None = None
_rag_singleton_lock = threading.RLock()


def get_rag_service(config: PaperFlowConfig | None = None) -> RAGService:
    """模块级单例：indexer 与 retriever 必须共享同一实例（热更新前提）。"""
    global _rag_service
    if _rag_service is None:
        with _rag_singleton_lock:
            if _rag_service is None:
                _rag_service = RAGService(config or PaperFlowConfig.from_env())
    return _rag_service
