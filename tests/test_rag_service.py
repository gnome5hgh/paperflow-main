# tests/test_rag_service.py
from paperflow.config import PaperFlowConfig
from paperflow.rag.service import get_rag_service


def test_get_rag_service_singleton():
    cfg = PaperFlowConfig(workspace="data", chroma_path="")
    a = get_rag_service(cfg)
    b = get_rag_service(cfg)
    assert a is b


def test_service_has_lock_and_lazy_components():
    from paperflow.config import PaperFlowConfig
    from paperflow.rag.service import RAGService
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
    from paperflow.rag.service import RAGService
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
