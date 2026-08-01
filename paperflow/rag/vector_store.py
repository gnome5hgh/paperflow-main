"""ChromaDB 封装：单 collection，documents 存块原文，metadata 只留 source/path/mtime。"""
import numpy as np
import chromadb

from paperflow.rag.chunker import Chunk


class VectorStore:
    def __init__(self, path: str, collection_name: str = "paperflow"):
        # PersistentClient 直接指向目录；测试传 tmp_path 目录
        self._client = chromadb.PersistentClient(path=path)
        self._col = self._client.get_or_create_collection(collection_name)

    def upsert(self, chunks: list[Chunk], embeddings: np.ndarray, mtime: float = 0.0) -> None:
        """写入/覆盖块：documents 存原文（query 默认返回 + BM25 重建源）。"""
        self._col.upsert(
            ids=[c.id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=embeddings.tolist(),
            metadatas=[{"source": c.source, "path": c.path, "mtime": mtime} for c in chunks],
        )

    def query(self, embedding: np.ndarray, top_k: int) -> list[tuple[str, str, float]]:
        res = self._col.query(query_embeddings=[embedding.tolist()], n_results=top_k)
        ids = res["ids"][0]
        docs = res["documents"][0]
        dists = res["distances"][0]
        return list(zip(ids, docs, dists))

    def delete_doc(self, path: str) -> None:
        # where 过滤删除该文档全部块（ChromaDB 按 metadata 过滤）
        self._col.delete(where={"path": path})

    def all_documents(self) -> list[tuple[str, str, str, float]]:
        """返回全部块 (id, document, path, mtime)——BM25 重建与 state 比对的数据源。"""
        res = self._col.get(include=["documents", "metadatas"])
        out = []
        for i, doc_id in enumerate(res["ids"]):
            md = res["metadatas"][i] or {}
            out.append((doc_id, res["documents"][i], md.get("path", ""), md.get("mtime", 0.0)))
        return out

    def count(self) -> int:
        return self._col.count()
