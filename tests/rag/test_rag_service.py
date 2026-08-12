# tests/test_rag_service.py
import time

from paperflow.config import PaperFlowConfig
from paperflow.rag.services.rag_service import get_rag_service


def test_get_rag_service_singleton():
    cfg = PaperFlowConfig(workspace="data", chroma_path="")
    a = get_rag_service(cfg)
    b = get_rag_service(cfg)
    assert a is b


def test_service_has_lock_and_lazy_components():
    from paperflow.config import PaperFlowConfig
    from paperflow.rag.services.rag_service import RAGService
    s = RAGService(PaperFlowConfig())
    assert hasattr(s, "lock")
    # 惰性：未访问组件前不实例化
    assert s._embedder is None
    assert s._vector_store is None


def test_service_index_all_entrypoint_holds_lock(tmp_path):
    # IMPORTANT-2 回归：index_all 便捷入口必须持锁（与 index_document/retrieve 同级），
    # 不得绕过锁直接调 get_indexer().index_all()——否则并发下 BM25 重建/ChromaDB
    # 全量写与查询读半截状态。用 TrackingLock 断言 `with self.lock` 确实包裹了
    # indexer.index_all 的执行（threading.RLock 无公开 locked()，故用包装锁计数）。
    import threading
    from paperflow.config import PaperFlowConfig
    from paperflow.rag.services.rag_service import RAGService
    note_dir = tmp_path / "note"; note_dir.mkdir(parents=True)
    svc = RAGService(PaperFlowConfig(
        workspace=str(tmp_path / "ws"),
        vault_note_dir=str(note_dir),
        vault_pdf_dir=str(tmp_path / "pdf"),
        chroma_path=str(tmp_path / "chroma"),
    ))
    (tmp_path / "ws").mkdir(exist_ok=True)

    class TrackingLock:
        """包装 RLock：记录 `with` 进入次数（__enter__ 被调用即证明持锁）。"""
        def __init__(self):
            self.enters = 0
            self._inner = threading.RLock()
        def __enter__(self):
            self.enters += 1
            return self._inner.__enter__()
        def __exit__(self, *exc):
            return self._inner.__exit__(*exc)

    svc.lock = TrackingLock()
    captured = {}
    class SpyIndexer:
        def index_all(self):
            captured["lock_enters"] = svc.lock.enters   # 执行时 lock 已进入 → ≥1
    svc._indexer = SpyIndexer()
    svc.index_all()
    assert captured.get("lock_enters") == 1             # indexer 执行期间锁被持有


def _make_svc(tmp_path):
    from paperflow.rag.services.rag_service import RAGService
    from paperflow.config import PaperFlowConfig
    return RAGService(PaperFlowConfig(workspace=str(tmp_path / "ws")))


def test_parse_pdf_cached_parses_once(tmp_path):
    """缓存命中：同 path+mtime+size 二次调用不重新解析。"""
    from paperflow.rag.parsers.grobid_client import ParsedDoc
    svc = _make_svc(tmp_path)
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"x" * 100)
    calls = []
    class CountingParser:
        def parse_pdf(self, path):
            calls.append(path)
            return ParsedDoc(sections=[("A", "t")], tables=[], figures=[])
    svc._pymupdf_parser = CountingParser()
    svc._grobid_available = False
    d1 = svc.parse_pdf_cached(str(pdf))
    d2 = svc.parse_pdf_cached(str(pdf))
    assert len(calls) == 1
    assert d1 is d2


def test_parse_pdf_cached_invalidates_on_content_change(tmp_path):
    """mtime+size 变化（PDF 替换）→ 缓存失效重解析，零代码维护。"""
    from paperflow.rag.parsers.grobid_client import ParsedDoc
    svc = _make_svc(tmp_path)
    pdf = tmp_path / "p.pdf"
    calls = []
    class CountingParser:
        def parse_pdf(self, path):
            calls.append(path)
            return ParsedDoc(sections=[("A", "t")], tables=[], figures=[])
    svc._pymupdf_parser = CountingParser()
    svc._grobid_available = False
    pdf.write_bytes(b"x" * 100)
    svc.parse_pdf_cached(str(pdf))
    pdf.write_bytes(b"y" * 200)          # 内容变 → mtime_ns 与 size 都变 → 失效
    svc.parse_pdf_cached(str(pdf))
    assert len(calls) == 2


def test_parse_pdf_cache_exception_not_cached(tmp_path):
    """GROBID 异常不缓存：下次调用重试（故障不固化，D4）。"""
    import pytest
    from paperflow.rag.parsers.grobid_client import ParsedDoc
    svc = _make_svc(tmp_path)
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"x" * 100)
    class FlakyParser:
        """第一次抛异常模拟 GROBID 故障，第二次成功——验证失败不被缓存。"""
        def __init__(self):
            self.n = 0
        def parse_pdf(self, path):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("grobid down")
            return ParsedDoc(sections=[("A", "t")], tables=[], figures=[])
    svc._pymupdf_parser = FlakyParser()
    svc._grobid_available = False
    with pytest.raises(RuntimeError):
        svc.parse_pdf_cached(str(pdf))
    doc = svc.parse_pdf_cached(str(pdf))   # 第二次成功 → 失败未被缓存
    assert doc.sections[0][0] == "A"


def test_parse_pdf_cached_concurrent_single_parse(tmp_path):
    """双检锁：N 线程并发首 miss → 只解析一次（持锁解析，GROBID 单服务本就串行）。"""
    import threading
    from paperflow.rag.parsers.grobid_client import ParsedDoc
    svc = _make_svc(tmp_path)
    pdf = tmp_path / "p.pdf"
    pdf.write_bytes(b"x" * 100)
    calls, cl = [], threading.Lock()
    class SlowParser:
        def parse_pdf(self, path):
            with cl:
                calls.append(path)
            time.sleep(0.2)               # 放大竞态窗口
            return ParsedDoc(sections=[("A", "t")], tables=[], figures=[])
    svc._pymupdf_parser = SlowParser()
    svc._grobid_available = False
    barrier = threading.Barrier(4)
    def worker():
        barrier.wait()
        svc.parse_pdf_cached(str(pdf))
    ts = [threading.Thread(target=worker) for _ in range(4)]
    for t in ts: t.start()
    for t in ts: t.join()
    assert len(calls) == 1
