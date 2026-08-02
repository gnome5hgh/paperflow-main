# tests/test_rag_indexer.py
import json
import time
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.rag.embedder import FakeEmbedder
from paperflow.rag.indexer import RagIndexer
from paperflow.rag.service import RAGService


def _make_service(tmp_path, note_dir, pdf_dir):
    cfg = PaperFlowConfig(
        workspace=str(tmp_path / "ws"),
        vault_note_dir=str(note_dir), vault_pdf_dir=str(pdf_dir),
        chroma_path=str(tmp_path / "chroma"),
    )
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    return RAGService(cfg)


def test_index_all_indexes_note(tmp_path):
    note_dir = tmp_path / "vault" / "note"
    note_dir.mkdir(parents=True)
    (note_dir / "a.md").write_text("# 标题\n\ncircRNA 调控内容。", encoding="utf-8")
    svc = _make_service(tmp_path, note_dir, tmp_path / "vault" / "pdf")
    svc._embedder = FakeEmbedder()          # 注入测试替身
    idx = RagIndexer(svc)
    idx.index_all()
    assert svc._vector_store.count() > 0    # 访问触发惰性构造


def test_index_document_then_state_recorded(tmp_path):
    note_dir = tmp_path / "vault" / "note"
    note_dir.mkdir(parents=True)
    p = note_dir / "b.md"
    p.write_text("miRNA 网络。", encoding="utf-8")
    svc = _make_service(tmp_path, note_dir, tmp_path / "vault" / "pdf")
    svc._embedder = FakeEmbedder()
    RagIndexer(svc).index_document(str(p))
    state = json.loads((tmp_path / "ws" / "index_state.json").read_text())
    assert str(p.resolve()) in state


def test_index_all_skips_unchanged(tmp_path):
    note_dir = tmp_path / "vault" / "note"
    note_dir.mkdir(parents=True)
    p = note_dir / "c.md"
    p.write_text("hello", encoding="utf-8")
    svc = _make_service(tmp_path, note_dir, tmp_path / "vault" / "pdf")
    svc._embedder = FakeEmbedder()
    idx = RagIndexer(svc)
    idx.index_all()
    first = svc._vector_store.count()
    idx.index_all()          # 二次扫描：mtime 未变，不重索引
    assert svc._vector_store.count() == first


def test_authority_collection_empty_state_nonempty_rescans(tmp_path):
    note_dir = tmp_path / "vault" / "note"
    note_dir.mkdir(parents=True)
    (note_dir / "d.md").write_text("fresh", encoding="utf-8")
    svc = _make_service(tmp_path, note_dir, tmp_path / "vault" / "pdf")
    svc._embedder = FakeEmbedder()
    idx = RagIndexer(svc)
    # 模拟：state 存在但 collection 被清空（如 chromadb/ 目录被删）
    idx._save_state({"old/path.md": 123.0})
    idx.index_all()
    assert svc._vector_store.count() > 0    # 触发全量重扫而非跳过
