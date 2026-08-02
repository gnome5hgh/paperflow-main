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
