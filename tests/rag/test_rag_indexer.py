# tests/test_rag_indexer.py
import json
import os
import time
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.rag.encoders.embedder import FakeEmbedder
from paperflow.rag.services.indexer import RagIndexer
from paperflow.rag.encoders.reranker import FakeReranker
from paperflow.rag.services.rag_service import RAGService


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


# ─── CRITICAL-1 回归：memory 根不得进 RAG 索引（SCOPE 只索引 note/pdf）────────

def test_index_document_memory_root_is_noop(tmp_path):
    # memory 文件（非 vault 根）index_document → no-op：向量库/BM25 计数不变。
    # 修复前 _rel_path 回退 basename → memory/shared.md 与 note/shared.md
    # 同 doc id，index_document(memory) 静默覆盖并删除 note chunks（数据丢失）。
    note_dir = tmp_path / "vault" / "note"; note_dir.mkdir(parents=True)
    pdf_dir = tmp_path / "vault" / "pdf"
    svc = _make_service(tmp_path, note_dir, pdf_dir)
    svc._embedder = FakeEmbedder()
    idx = RagIndexer(svc)
    memory = tmp_path / "ws" / "memory" / "shared.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("# 记忆\n\n一些记忆内容", encoding="utf-8")
    idx.index_document(str(memory))
    assert svc._ensure_vector_store().count() == 0
    assert svc._ensure_bm25().count() == 0
    # state 也不应写入 memory 键（index_document 在写 state 前已 no-op）
    state = json.loads((tmp_path / "ws" / "index_state.json").read_text()) \
        if (tmp_path / "ws" / "index_state.json").exists() else {}
    assert str(memory.resolve()) not in state


def test_memory_note_same_basename_no_collision(tmp_path):
    # 同 basename：note/shared.md + memory/shared.md。先索引 note，再索引 memory
    # （修复前会覆盖并删除 note chunks）。修复后 note 的 chunks 仍在。
    note_dir = tmp_path / "vault" / "note"; note_dir.mkdir(parents=True)
    pdf_dir = tmp_path / "vault" / "pdf"
    svc = _make_service(tmp_path, note_dir, pdf_dir)
    svc._embedder = FakeEmbedder()
    idx = RagIndexer(svc)
    note = note_dir / "shared.md"
    note.write_text("# 笔记\n\ncircRNA 网络调控内容", encoding="utf-8")
    idx.index_document(str(note))
    note_count = svc._vector_store.count()
    assert note_count > 0
    memory = tmp_path / "ws" / "memory" / "shared.md"
    memory.parent.mkdir(parents=True)
    memory.write_text("# 记忆\n\n无关记忆内容", encoding="utf-8")
    idx.index_document(str(memory))            # 修复前这里覆盖并删除 note chunks
    assert svc._vector_store.count() == note_count
    assert svc._ensure_bm25().count() == note_count
    # note 的块仍在：metadata path 为相对 note 根的 shared.md，且文本未被 memory 内容替换
    note_docs = [d for d in svc._vector_store.all_documents()
                 if d[2] == "shared.md" and d[1].find("circRNA") >= 0]
    assert note_docs, "note 的 chunks 被 memory 覆盖/删除了（CRITICAL-1）"


def test_index_all_excludes_memory_and_note_retrievable(tmp_path):
    # index_all 只扫 note/pdf 根：memory 不进索引；note 仍可检索。
    # 同时覆盖：state 中残留 memory 键时孤儿清理对 rel=None 防御性 continue。
    note_dir = tmp_path / "vault" / "note"; note_dir.mkdir(parents=True)
    pdf_dir = tmp_path / "vault" / "pdf"
    svc = _make_service(tmp_path, note_dir, pdf_dir)
    svc._embedder = FakeEmbedder()
    svc._reranker = FakeReranker()             # retrieve 需要（不加载真实模型）
    idx = RagIndexer(svc)
    note = note_dir / "a.md"
    note.write_text("# 笔记\n\ncircRNA 可检索内容", encoding="utf-8")
    idx.index_all()
    assert svc._vector_store.count() > 0
    # 残留 memory 键（历史 bug 产物）→ 孤儿清理必须跳过，不误删不崩溃
    memory = tmp_path / "ws" / "memory" / "a.md"
    memory.parent.mkdir(parents=True)
    idx._save_state({str(memory): 1.0})
    memory.write_text("# 记忆\n\n不该进索引", encoding="utf-8")
    idx.index_all()                            # 修复前 memory/a.md 会覆盖 note/a.md
    assert svc._vector_store.count() > 0
    rels = {d[2] for d in svc._vector_store.all_documents()}
    assert rels == {"a.md"}                    # 只有 note 相对路径，无 memory 混入
    # note 仍可检索（BM25 + vector 双命中 note 内容）
    hits = svc.get_retriever().retrieve("circRNA 可检索内容", top_k=3)
    assert any("circRNA" in h.text for h in hits)
