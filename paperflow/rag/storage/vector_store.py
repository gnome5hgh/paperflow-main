"""向量库封装（基于 ChromaDB）：单一集合，文档字段存块的原文，元数据只存来源、路径、修改时间。

查询默认返回文档原文，供上层展示和 BM25 重建使用。
"""
import numpy as np
import chromadb

from paperflow.rag.parsers.chunker import Chunk


class VectorStore:
    """向量库的读写封装：写入/覆盖块、按向量检索、按路径删除、读取全部块。"""

    def __init__(self, path: str, collection_name: str = "paperflow"):
        """打开（必要时创建）指定目录下的向量库集合。path 指向一个持久化目录。"""
        # PersistentClient 直接指向目录；测试时传临时目录即可
        self._client = chromadb.PersistentClient(path=path)
        self._col = self._client.get_or_create_collection(collection_name)

    def upsert(self, chunks: list[Chunk], embeddings: np.ndarray, mtime: float = 0.0) -> None:
        """写入或覆盖一批块：同 id 的块会覆盖旧数据。

        documents 存原文，因为查询默认返回文档、且 BM25 要靠它重建。
        """
        self._col.upsert(
            ids=[c.id for c in chunks],
            documents=[c.text for c in chunks],
            embeddings=embeddings.tolist(),
            metadatas=[{"source": c.source, "path": c.path, "mtime": mtime} for c in chunks],
        )

    def query(self, embedding: np.ndarray, top_k: int) -> list[tuple[str, str, float]]:
        """按向量做相似度检索，返回前 top_k 条，每条为 (块 id, 原文, 距离)。"""
        res = self._col.query(query_embeddings=[embedding.tolist()], n_results=top_k)
        ids = res["ids"][0]
        docs = res["documents"][0]
        dists = res["distances"][0]
        return list(zip(ids, docs, dists))

    def delete_doc(self, path: str) -> None:
        """删除指定路径文档的全部块（按元数据里的 path 字段过滤）。"""
        # ChromaDB 按 metadata 过滤删除
        self._col.delete(where={"path": path})

    def all_documents(self) -> list[tuple[str, str, str, float]]:
        """返回全部块，每块为 (块 id, 原文, 路径, 修改时间)——BM25 重建和索引状态比对都依赖它。"""
        res = self._col.get(include=["documents", "metadatas"])
        out = []
        for i, doc_id in enumerate(res["ids"]):
            md = res["metadatas"][i] or {}
            out.append((doc_id, res["documents"][i], md.get("path", ""), md.get("mtime", 0.0)))
        return out

    def count(self) -> int:
        """集合中的块总数。"""
        return self._col.count()
