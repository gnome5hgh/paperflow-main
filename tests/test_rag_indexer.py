# tests/test_rag_indexer.py
import json
import os
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


def test_guard2_derives_state_and_skips_unchanged(tmp_path):
    note_dir = tmp_path / "vault" / "note"; note_dir.mkdir(parents=True)
    a = note_dir / "a.md"; a.write_text("# 标题\n\ncircRNA 内容", encoding="utf-8")
    # 冻结 a.md mtime：消除 APFS 亚秒级 st_mtime 方差——否则 guard-2 的
    # metadata mtime vs 磁盘 mtime 比对在负载下偶发不等，a.md 被误判"已变更"重 embedding，
    # 断言 calls == first_calls + 1 偶发失败（已两次复现）
    os.utime(a, ns=(1_000_000_000_000, 1_000_000_000_000))
    svc = _make_service(tmp_path, note_dir, tmp_path / "vault" / "pdf")
    svc._embedder = FakeEmbedder()
    idx = RagIndexer(svc)
    idx.index_all()
    first_calls = svc._embedder.calls
    (tmp_path / "ws" / "index_state.json").unlink()   # 模拟 state 丢失
    b = note_dir / "b.md"; b.write_text("# 新\n\nmiRNA 内容", encoding="utf-8")
    idx.index_all()
    # guard-2 从 metadata 重建 state → a.md 不变不重 embedding，只 embed b.md（1 个 text）
    assert svc._embedder.calls == first_calls + 1


def test_hot_update_no_bm25_accumulation(tmp_path):
    note_dir = tmp_path / "vault" / "note"; note_dir.mkdir(parents=True)
    p = note_dir / "n.md"; p.write_text("# H\n\nbody text", encoding="utf-8")
    svc = _make_service(tmp_path, note_dir, tmp_path / "vault" / "pdf")
    svc._embedder = FakeEmbedder()
    idx = RagIndexer(svc)
    idx.index_document(str(p))
    first = svc._ensure_bm25().count()
    p.write_text("# H\n\nbody text changed", encoding="utf-8")   # 同 chunk 结构编辑
    idx.index_document(str(p))
    assert svc._ensure_bm25().count() == first                  # dict 幂等不膨胀


def test_shrink_removes_old_chunks(tmp_path):
    note_dir = tmp_path / "vault" / "note"; note_dir.mkdir(parents=True)
    p = note_dir / "s.md"
    p.write_text("# 概述\n\n第一部分内容\n## 方法\n\n第二部分内容", encoding="utf-8")
    svc = _make_service(tmp_path, note_dir, tmp_path / "vault" / "pdf")
    svc._embedder = FakeEmbedder()
    idx = RagIndexer(svc)
    idx.index_document(str(p))
    before = svc._ensure_vector_store().count()
    p.write_text("# 概述\n\n只有概述了", encoding="utf-8")   # 方法 section 删除
    idx.index_document(str(p))
    after = svc._ensure_vector_store().count()
    assert after < before                                     # 旧块被清
    assert svc._ensure_bm25().count() == after               # BM25 投影一致
