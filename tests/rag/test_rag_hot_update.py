# tests/test_rag_hot_update.py
"""集成线②：写 note → index_document 热更新 → 检索命中该内容（热更新决策成立的关键路径）。"""
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.rag.encoders.embedder import FakeEmbedder
from paperflow.rag.encoders.reranker import FakeReranker
from paperflow.rag.services.rag_service import RAGService


def test_write_note_then_retrieve_hits(tmp_path):
    note_dir = tmp_path / "vault" / "note"
    note_dir.mkdir(parents=True)
    cfg = PaperFlowConfig(
        workspace=str(tmp_path / "ws"), chroma_path=str(tmp_path / "chroma"),
        vault_note_dir=str(note_dir), vault_pdf_dir=str(tmp_path / "vault" / "pdf"),
    )
    (tmp_path / "ws").mkdir(exist_ok=True)
    svc = RAGService(cfg)
    svc._embedder = FakeEmbedder()
    svc._reranker = FakeReranker()

    # 写入新笔记 → 模拟 WriteFileTool 的热更新钩子
    note = note_dir / "new.md"
    note.write_text("# 图对比学习\n\n异构图神经网络的链路预测方法。", encoding="utf-8")
    svc.index_document(str(note))

    # 同一 RAG 栈（单例）检索 → 必须命中刚写入的内容
    hits = svc.retrieve("异构图神经网络", top_k=1)
    assert len(hits) > 0
    assert "链路预测" in hits[0].text or "异构图" in hits[0].text
