# tests/test_rag_retriever.py
import numpy as np

from paperflow.config import PaperFlowConfig
from paperflow.rag.embedder import FakeEmbedder
from paperflow.rag.reranker import FakeReranker
from paperflow.rag.retriever import Retriever, RagRetrieveTool
from paperflow.rag.service import RAGService


def _seed_store(svc):
    """填充两条文档：一条含 circRNA，一条含 miRNA。"""
    from paperflow.rag.chunker import Chunk
    chunks = [
        Chunk(id="d1", text="circRNA 调控机制", path="note/a.md",
              source="note", heading="H", chunk_index=0),
        Chunk(id="d2", text="miRNA 表达分析", path="note/b.md",
              source="note", heading="H", chunk_index=0),
    ]
    svc._embedder = FakeEmbedder()
    vecs = svc._embedder([c.text for c in chunks])
    svc._ensure_vector_store().upsert(chunks, vecs, mtime=1.0)
    svc._ensure_bm25().rebuild([(c.id, c.text) for c in chunks])
    return chunks


def _make_svc(tmp_path):
    cfg = PaperFlowConfig(workspace=str(tmp_path / "ws"), chroma_path=str(tmp_path / "chroma"))
    (tmp_path / "ws").mkdir(exist_ok=True)
    svc = RAGService(cfg)
    svc._reranker = FakeReranker()
    return svc


def test_retrieve_rrf_returns_chunks(tmp_path):
    svc = _make_svc(tmp_path)
    _seed_store(svc)
    r = Retriever(svc)
    hits = r.retrieve("circRNA 机制", top_k=2)
    assert len(hits) > 0
    assert all(isinstance(c.text, str) for c in hits)


def test_retrieve_empty_bm25_falls_back_to_vector(tmp_path):
    # 集成线①：BM25 空 → 纯 Vector 回退全链路可用
    svc = _make_svc(tmp_path)
    _seed_store(svc)
    svc._ensure_bm25().rebuild([])          # 模拟 BM25 空（仅向量有数据）
    r = Retriever(svc)
    hits = r.retrieve("circRNA", top_k=1)
    assert len(hits) >= 1                    # 向量侧仍能命中


def test_rag_retrieve_tool_execute(tmp_path):
    svc = _make_svc(tmp_path)
    _seed_store(svc)
    tool = RagRetrieveTool()
    tool._service = svc                      # 测试注入服务
    result = tool.execute(query="circRNA", top_k=1)
    # FakeReranker 按 md5 稳定排序、与 query 语义无关，top-1 可能是两个种子块之一，
    # 故断言"检索命中"成功前缀而非具体块内容——验证端到端契约而非 md5 大小。
    assert "检索到以下相关段落" in result.text
