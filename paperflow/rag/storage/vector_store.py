"""向量库封装（基于 Milvus）：单一 collection，text 字段存块的原文，元数据存来源、路径、修改时间。

同一个 ``MilvusClient`` 类覆盖两种部署：``uri`` 传本地文件路径即 Milvus Lite
（内嵌、单测用，无需常驻服务），传 ``http://host:19530`` 即连 Milvus Standalone
（生产）。查询默认返回文档原文，供上层展示和 BM25 重建使用。

并发约定：本类方法假定调用方已持外部锁（RAGService.lock）——Milvus 客户端
本身非线程安全，串行访问由上层保证。
"""
from pymilvus import DataType, MilvusClient

from paperflow.rag.parsers.chunker import Chunk


class VectorStore:
    """向量库的读写封装：写入/覆盖块、按向量检索、按路径删除、读取全部块。"""

    def __init__(self, uri: str, dim: int, collection_name: str = "paperflow",
                 batch_size: int = 1000):
        """打开（必要时创建）指定 uri 的向量库集合。

        Args:
            uri: Milvus 连接地址。本地文件路径 → Milvus Lite（内嵌）；
                 ``http://host:port`` → Milvus Standalone/分布式。
            dim: 向量维度，必须与写入的 embedding 维度一致（建集合时定死）。
            collection_name: 集合名，默认 ``paperflow``。
            batch_size: all_documents 分页遍历的每页行数（测试可传小值验证跨页）。
        """
        self._client = MilvusClient(uri=uri)
        self._collection = collection_name
        self._batch_size = batch_size
        if not self._client.has_collection(collection_name):
            self._create_collection(dim)
        # 确保集合已加载进内存供检索（Standalone 必需，Lite 幂等无害）
        self._client.load_collection(collection_name)

    def _create_collection(self, dim: int) -> None:
        """按固定 schema 建集合：id 主键 + 向量 + 原文/来源/路径/修改时间。"""
        schema = self._client.create_schema(auto_id=False)
        # id 是 16 位 sha1，128 足够；text 存块原文供 BM25 重建与结果展示
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("source", DataType.VARCHAR, max_length=16)
        schema.add_field("path", DataType.VARCHAR, max_length=1024)
        # mtime 必须 DOUBLE：float32 在 1.7e9 量级精度不足，增量比对会失真
        schema.add_field("mtime", DataType.DOUBLE)

        index_params = self._client.prepare_index_params()
        # 向量索引：HNSW + COSINE（bge 向量已 L2 归一化）
        index_params.add_index(
            field_name="vector", index_type="HNSW", metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        # path 标量索引：供 delete_doc / all_documents 按路径过滤
        index_params.add_index(field_name="path", index_type="INVERTED")
        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index_params,
        )

    def upsert(self, chunks: list[Chunk], embeddings, mtime: float = 0.0) -> None:
        """写入或覆盖一批块：同 id 的块覆盖旧数据（主键幂等）。

        ``text`` 存原文（查询默认返回文档、BM25 靠它重建）。写后 ``flush``
        让 ``count()``/统计立刻反映新数据，否则只计已落盘段、一致性修复读到过期计数。
        """
        data = [
            {
                "id": c.id,
                "vector": embeddings[i].tolist(),
                "text": c.text,
                "source": c.source,
                "path": c.path,
                "mtime": float(mtime),
            }
            for i, c in enumerate(chunks)
        ]
        self._client.upsert(collection_name=self._collection, data=data)
        self._client.flush(self._collection)

    def query(self, embedding, top_k: int) -> list[tuple[str, str, float]]:
        """按向量做相似度检索，返回前 top_k 条，每条为 (块 id, 原文, 距离)。"""
        res = self._client.search(
            collection_name=self._collection,
            data=[embedding.tolist()],
            limit=top_k,
            output_fields=["text"],
        )
        # res[0] 是第一条 query 的命中列表；每项含 id / distance / entity
        return [(h["id"], h["entity"]["text"], h["distance"]) for h in res[0]]

    def delete_doc(self, path: str) -> None:
        """删除指定路径文档的全部块（按 path 字段过滤）。

        路径里的反斜杠与双引号先转义，避免破坏过滤表达式。写后 ``flush``
        让 ``count()`` 立刻反映删除。
        """
        escaped = path.replace("\\", "\\\\").replace('"', '\\"')
        self._client.delete(
            collection_name=self._collection, filter=f'path == "{escaped}"',
        )
        self._client.flush(self._collection)

    def all_documents(self) -> list[tuple[str, str, str, float]]:
        """返回全部块，每块为 (块 id, 原文, 路径, 修改时间)。

        用 ``query_iterator`` 分页遍历——Milvus 单次 query 有 16384 行上限，
        超过会被截断；迭代器无此限制。BM25 重建与索引状态比对都依赖本方法。
        """
        out: list[tuple[str, str, str, float]] = []
        it = self._client.query_iterator(
            collection_name=self._collection,
            filter="",
            output_fields=["text", "path", "mtime"],
            batch_size=self._batch_size,
        )
        while True:
            try:
                batch = it.next()
            except StopIteration:
                break
            if not batch:
                break
            for row in batch:
                out.append((row["id"], row["text"], row["path"], float(row["mtime"])))
        it.close()
        return out

    def count(self) -> int:
        """集合中的块总数。"""
        stats = self._client.get_collection_stats(self._collection)
        return int(stats.get("row_count", 0))
