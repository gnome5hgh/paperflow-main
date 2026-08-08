# tests/test_rag_vector_store.py
import numpy as np
from paperflow.rag.chunker import Chunk
from paperflow.rag.vector_store import VectorStore


def _chunk(i: int, path: str = "note/a.md") -> Chunk:
    return Chunk(id=f"id{i}", text=f"text {i}", path=path,
                 source="note", heading="H", chunk_index=i)


def test_upsert_query_roundtrip(tmp_path):
    vs = VectorStore(str(tmp_path / "chroma"))
    chunks = [_chunk(0), _chunk(1)]
    vs.upsert(chunks, np.array([[1.0, 0.0], [0.0, 1.0]], dtype=float))
    results = vs.query(np.array([1.0, 0.0], dtype=float), top_k=1)
    assert results[0][0] == "id0"
    assert results[0][1] == "text 0"          # documents 字段返回原文


def test_all_documents_for_bm25_rebuild(tmp_path):
    vs = VectorStore(str(tmp_path / "chroma"))
    vs.upsert([_chunk(0), _chunk(1)], np.array([[1.0, 0.0], [0.0, 1.0]], dtype=float))
    docs = vs.all_documents()
    assert {(d[0], d[1]) for d in docs} == {("id0", "text 0"), ("id1", "text 1")}


def test_delete_doc(tmp_path):
    vs = VectorStore(str(tmp_path / "chroma"))
    vs.upsert([_chunk(0, "note/a.md"), _chunk(1, "note/b.md")],
              np.array([[1.0, 0.0], [0.0, 1.0]], dtype=float))
    vs.delete_doc("note/a.md")
    docs = vs.all_documents()
    assert [d[1] for d in docs] == ["text 1"]
    assert vs.count() == 1
