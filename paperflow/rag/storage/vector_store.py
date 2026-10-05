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

    def __init__(self, uri: str, dim: int, collection_name: str, batch_size: int):
        """打开（必要时创建）指定 uri 的向量库集合。

        生产值来自 ``rag.storage.*``（唯一声明点 config.py，RagService 注入）。

        Args:
            uri: Milvus 连接地址。本地文件路径 → Milvus Lite（内嵌）；
                 ``http://host:port`` → Milvus Standalone/分布式。
            dim: 向量维度，必须与写入的 embedding 维度一致（建集合时定死）。
            collection_name: 集合名。
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
        """按固定 schema 创建集合，同时建立向量索引和标量索引。

        Schema 字段说明：
        - id (VARCHAR, 主键): 块 ID，16 位 sha1 哈希值，长度 128 足够。
        - vector (FLOAT_VECTOR): 稠密向量，维度由参数 dim 指定。
        - text (VARCHAR): 块原文，最大 65535 字符，供 BM25 重建和结果展示。
        - source (VARCHAR): 来源类型，'note' 或 'pdf'，最大 16 字符。
        - path (VARCHAR): 相对路径，最大 1024 字符，用于按文档删除和元数据展示。
        - mtime (DOUBLE): 修改时间戳。**必须用 DOUBLE**：float32 在 1.7e9（2023年时间戳）
                           量级精度不足（只能精确到秒级整数），增量比对会失真。

        Args:
            dim: 向量维度。
        """
        schema = self._client.create_schema(auto_id=False)

        # ---- 字段定义 ----
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("source", DataType.VARCHAR, max_length=16)
        schema.add_field("path", DataType.VARCHAR, max_length=1024)
        schema.add_field("mtime", DataType.DOUBLE)

        # ---- 索引配置 ----
        index_params = self._client.prepare_index_params()

        # 向量索引：HNSW + COSINE 距离。BGE 向量已 L2 归一化，COSINE 等价于内积。
        index_params.add_index(
            field_name="vector",
            index_type="HNSW",
            metric_type="COSINE", # 余弦相似度，与 L2 归一化向量配合
            params={"M": 16, "efConstruction": 200}, # M=16（每层连接数），efConstruction=200（构建时搜索宽度）是平衡性能的常用配置。
        )

        # path 标量索引：供 delete_doc / all_documents 按路径过滤
        index_params.add_index(field_name="path", index_type="INVERTED")

        self._client.create_collection(
            collection_name=self._collection, schema=schema, index_params=index_params,
        )

    def upsert(self, chunks: list[Chunk], embeddings, mtime: float = 0.0) -> None:
        """写入或覆盖一批块：同 id 的块覆盖旧数据（主键幂等）。

        关键设计：
        - ``text`` 字段存原文，因为查询默认返回文档、BM25 重建也依赖它。
        - 使用 ``upsert`` 而非 ``insert``，实现按主键的幂等覆盖。
        - 写入后调用 ``flush()`` 强制落盘，使 ``count()`` 和 ``get_collection_stats``
          立刻反映新数据。若不 flush，统计数据可能滞后（只计已落盘段），
          导致索引一致性修复逻辑读到过期的 row_count。

        Args:
            chunks: Chunk 对象列表。
            embeddings: 对应的向量矩阵（numpy.ndarray 或 list），形状为 (len(chunks), dim)。
            mtime: 文档修改时间戳（浮点数），同一文档的所有块共享此值。
        """
        data = [
            {
                "id": c.id,
                "vector": embeddings[i].tolist(), # 转换为 Python list
                "text": c.text,
                "source": c.source,
                "path": c.path,
                "mtime": float(mtime), # 确保为 float 类型
            }
            for i, c in enumerate(chunks)
        ]
        self._client.upsert(collection_name=self._collection, data=data)
        self._client.flush(self._collection)

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        """转义 Milvus 过滤表达式里的字符串值：反斜杠与双引号。

        例如 "C:\\Users\\a.md" → "C:\\\\Users\\a.md"，否则破坏 filter 语法。
        从 delete_doc 抽出，供按值过滤的查询共用。
        """
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def query(self, embedding, top_k: int, expr: str = "") -> list[tuple[str, str, str, str, float]]:
        """按向量相似度检索，返回前 top_k 条 (块 id, 原文, 相对路径, 来源, 距离)。

        expr 为 Milvus 过滤表达式（如 'source == "note"'），空串不过滤。
        路径与来源直接随搜索结果带回（output_fields），检索路径不再需要
        全表扫描补元数据；COSINE 度量下 distance 越接近 1 越相似。

        Args:
            embedding: 查询向量，一维数组（numpy.ndarray 或 list）。
            top_k: 返回结果数量上限。
            expr: Milvus 标量过滤表达式，空串表示不过滤。

        Returns:
            list[tuple[str, str, str, str, float]]: 每条为
            (块 id, 原文, 相对路径, 来源, 距离分数)。
        """
        res = self._client.search(
            collection_name=self._collection,
            data=[embedding.tolist()], # 二维：[[v1, v2, ...]]
            limit=top_k,
            filter=expr, # 空串 = 不过滤
            output_fields=["text", "path", "source"], # 元数据随搜索结果带回，避免全表扫描补齐
        )

        # Milvus 的 ``search`` 返回格式是嵌套结构：
        # - 外层列表：每个 query 向量对应一个元素（这里只有 1 个）。
        # - 内层列表：命中结果列表，每项包含 id、distance、entity 等字段。
        # res[0] 是第一个（也是唯一一个）query 的命中列表；每项含 id / distance / entity
        return [(h["id"], h["entity"]["text"], h["entity"]["path"],
                 h["entity"]["source"], h["distance"]) for h in res[0]]

    def fetch_by_ids(self, ids: list[str]) -> list[tuple[str, str, str, str]]:
        """按块 id 批量定点取回 (id, 原文, 路径, 来源)，供 BM25 命中补齐文本。

        替代「全表扫描后按 id 过滤」：BM25 命中数 ≤ 30，定点 query 开销可忽略。
        不存在的 id 静默跳过；空列表直接返回空（不发请求）。

        Args:
            ids: 块 id 列表。

        Returns:
            list[tuple[str, str, str, str]]: 每条为 (id, text, path, source)，
            仅包含实际存在的 id，顺序由 Milvus 返回顺序决定。
        """
        if not ids:
            return []
        quoted = ", ".join(f'"{self._escape_filter_value(i)}"' for i in ids)
        res = self._client.query(
            collection_name=self._collection,
            filter=f"id in [{quoted}]",
            output_fields=["text", "path", "source"],
        )
        return [(r["id"], r["text"], r["path"], r["source"]) for r in res]

    def doc_chunk_ids(self, path: str) -> list[str]:
        """按文档相对路径取回其全部块 id（索引器「先删后建」与删除清理用）。

        Args:
            path: 文档的相对路径（存储时使用的路径值）。

        Returns:
            list[str]: 该路径下全部块 id；路径不存在时返回空列表。
        """
        res = self._client.query(
            collection_name=self._collection,
            filter=f'path == "{self._escape_filter_value(path)}"',
            output_fields=["id"],
        )
        return [r["id"] for r in res]

    def delete_doc(self, path: str) -> None:
        """删除指定路径文档的全部块（按 path 字段过滤）。

        Args:
            path: 文档的相对路径（存储时使用的路径值）。
        """
        # 过滤表达式是字符串拼接形式，path 值中的反斜杠（Windows 路径）和双引号必须转义，
        # 否则会破坏 filter 语法——转义逻辑统一收口在 _escape_filter_value。
        escaped = self._escape_filter_value(path)
        self._client.delete(
            collection_name=self._collection, filter=f'path == "{escaped}"',
        )
        # 写入后调用 flush，使 count() 立即反映删除结果。
        self._client.flush(self._collection)

    def all_documents(self) -> list[tuple[str, str, str, float]]:
        """返回全部块，每块为 (块 id, 原文, 路径, 修改时间)。

        为什么用 query_iterator 分页：
        - Milvus 单次 ``query`` 操作有 16384 行的返回上限（默认配置），
          超过限制会被静默截断，导致全量读取不完整。
        - ``query_iterator`` 自动分页，无此限制，可以遍历全部数据。

        使用场景：
        - BM25 索引重建：从向量库读取全部块的文本。
        - 索引状态重建：从元数据恢复 state 文件。

        Returns:
            list[tuple[str, str, str, float]]: 列表，每项为 (id, text, path, mtime)。
        """
        out: list[tuple[str, str, str, float]] = []
        it = self._client.query_iterator(
            collection_name=self._collection,
            filter="", # 空过滤 = 全量
            output_fields=["text", "path", "mtime"],
            batch_size=self._batch_size,
        )

        try:
            while True:
                try:
                    batch = it.next()
                except StopIteration:
                    break
                if not batch:
                    break
                for row in batch:
                    out.append((row["id"], row["text"], row["path"], float(row["mtime"])))
        finally:
            # 分页中途抛非 StopIteration 异常也要关闭迭代器，避免游标泄漏
            it.close()
        return out

    def count(self) -> int:
        """集合中的块总数。

        注意：该值依赖最后一次 flush 后的统计信息。写入/删除后调用 flush，可保证 count 立即反映最新状态。

        Returns:
            int: 集合中的记录条数。
        """
        stats = self._client.get_collection_stats(self._collection)
        return int(stats.get("row_count", 0))
