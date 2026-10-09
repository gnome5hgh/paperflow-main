"""向量库封装（基于 Milvus）：单一 collection，text 字段存块的正文，其余字段存块元数据。

同一个 ``MilvusClient`` 类覆盖两种部署：``uri`` 传本地文件路径即 Milvus Lite
（内嵌、单测用，无需常驻服务），传 ``http://host:19530`` 即连 Milvus Standalone
（生产）。查询默认返回块正文与元数据，供上层展示和 BM25 重建使用。

并发约定：本类方法假定调用方已持外部锁（RAGService.lock）——Milvus 客户端
本身非线程安全，串行访问由上层保证。

超时约定：每个 RPC 都要传超时（`rag.storage.timeout` / `write_timeout`）。这不只是「别等太久」
——pymilvus 默认会对失败 RPC 重试最多 75 次、退避到 3 秒，服务不可达时一次调用能白等好几分钟。
这个 timeout 同时是单次尝试的 gRPC 截止时间与整个重试循环的预算（pymilvus 从同一个参数取两者），
所以设了就等于给这次调用封了顶，失败立刻回到上层交给熔断器判断。
"""
import json
import logging
import time

from pymilvus import DataType, MilvusClient

from paperflow.rag.parsers.chunker import Chunk

logger = logging.getLogger(__name__)

#: 读路径要取回的块字段：检索结果直接交给上层，缺字段会让上层拿不到元数据。
_CHUNK_OUTPUT_FIELDS = ("text", "path", "title", "heading", "caption",
                        "chunk_type", "page_num_int", "position_int")

#: 建集合与启动校验共用的字段名集合（改这里等于改集合结构：老集合会被重建）。
_REQUIRED_FIELDS = (
    "id", "vector", "text", "path", "mtime",
    "title", "heading", "caption", "chunk_type",
    "page_num_int", "top_int", "position_int", "created_at",
)

#: 位置数组字段的容量上限：ARRAY 字段在 Milvus 里必须给定 ``max_capacity``。
#: 一个块对应一个章节区间，跨页时每页一组 (页, 四边坐标)，几十页的章节也用不到这么大。
_POSITION_CAPACITY = 512

#: 页码数组字段的容量上限（块覆盖到的页数）。
_PAGE_CAPACITY = 64


def _to_chunk(row: dict, chunk_id: str = "") -> Chunk:
    """把向量库的一行还原成 Chunk（位置数组按每 5 个一组还原成元组序列）。

    Args:
        row: Milvus 返回的行（键为字段名，含 ``_CHUNK_OUTPUT_FIELDS`` 里的字段）。
        chunk_id: 块主键。检索结果的实体里不一定带主键（主键在命中对象的外层），
            故由调用方单独传；缺省时退回行里的 ``id``。

    Returns:
        Chunk: 还原出的块；位置字段为空时 ``position`` 为空元组。
    """
    flat = list(row.get("position_int") or [])
    position = tuple(tuple(flat[i:i + 5]) for i in range(0, len(flat) - 4, 5))
    return Chunk(
        id=chunk_id or row.get("id", ""), text=row.get("text", ""),
        path=row.get("path", ""), title=row.get("title", ""),
        heading=row.get("heading", ""), caption=row.get("caption", ""),
        chunk_type=row.get("chunk_type", "text"), position=position,
    )


class VectorStore:
    """向量库的读写封装：写入/覆盖块、按向量检索、按路径删除、读取全部块。

    Attributes:
        _client: MilvusClient，向量库客户端（本地文件路径走 Milvus Lite，http 走 Standalone）
        _collection: str，集合名
        _batch_size: int，all_documents 分页遍历的每页行数
        _read_timeout: float | None，读路径单次 RPC 截止时间（秒）
        _write_timeout: float | None，写路径单次 RPC 截止时间（秒）
    """

    def __init__(self, uri: str, dim: int, collection_name: str, batch_size: int,
                 read_timeout: float | None = None,
                 write_timeout: float | None = None):
        """打开（必要时创建）指定 uri 的向量库集合。

        生产值来自 ``rag.storage.*``（唯一声明点 config.py，RagService 注入）。

        集合已存在时会比对所需字段：缺字段说明是旧结构（如新增元数据之前的集合），
        此时删集合重建并告警——不能只记一行日志继续跑，那会让后续写入撞上未知字段。
        重建后库是空的，需要调用方跑一次全量索引收敛（调用方从日志看得到）。

        Args:
            uri: Milvus 连接地址。本地文件路径 → Milvus Lite（内嵌）；
                 ``http://host:port`` → Milvus Standalone/分布式。
            dim: 向量维度，必须与写入的 embedding 维度一致（建集合时定死）。
            collection_name: 集合名。
            batch_size: all_documents 分页遍历的每页行数（测试可传小值验证跨页）。
            read_timeout: 读路径单次 RPC 截止时间（秒）；None 用 pymilvus 默认值
                 ——默认那套重试策略在服务不可达时会白等很久，生产不建议留空。
            write_timeout: 写路径单次 RPC 截止时间（秒）；批量入库本身耗时，故比读路径宽松。
        """
        self._client = MilvusClient(uri=uri)
        self._collection = collection_name
        self._batch_size = batch_size
        self._read_timeout = read_timeout
        self._write_timeout = write_timeout

        if self._client.has_collection(collection_name, timeout=read_timeout):
            stale = self._stale_fields()
            if stale:
                logger.warning(
                    "向量库集合 %s 的结构已过期（缺字段：%s），删除重建；"
                    "库现在是空的，请跑一次全量索引（reindex_all）重新入库。",
                    collection_name, "、".join(stale))
                self._client.drop_collection(collection_name, timeout=write_timeout)
                self._create_collection(dim)
        else:
            self._create_collection(dim)

        # 确保集合已加载进内存供检索（Standalone 必需，Lite 幂等无害）
        self._client.load_collection(collection_name, timeout=read_timeout)

    def _stale_fields(self) -> list[str]:
        """比对现有集合的字段名与所需字段，返回缺失的字段名列表。

        Returns:
            list[str]: 缺失字段名（空列表 = 结构齐备）。
        """
        try:
            desc = self._client.describe_collection(self._collection,
                                                    timeout=self._read_timeout)
        except Exception as e:
            # 读不出结构就不敢重建（可能是暂时不可达）——按「不重建」处理，
            # 让后续写入自己去暴露问题，比误删一个健康的集合安全。
            logger.warning("读取集合结构失败，跳过结构校验：%s", e)
            return []
        present = {f.get("name") for f in desc.get("fields", []) if isinstance(f, dict)}
        return [name for name in _REQUIRED_FIELDS if name not in present]

    def _create_collection(self, dim: int) -> None:
        """按固定 schema 创建集合，同时建立向量索引和标量索引。

        Schema 字段说明：
        - id (VARCHAR, 主键): 块 ID，16 位 sha1 哈希值，长度 128 足够。
        - vector (FLOAT_VECTOR): 稠密向量，维度由参数 dim 指定。
        - text (VARCHAR): 块正文，最大 65535 字符，供 BM25 重建和结果展示。
        - path (VARCHAR): 文档**绝对路径**，最大 1024 字符，用于按文档删除、元数据展示。
        - mtime (DOUBLE): 文档修改时间戳。**必须用 DOUBLE**：float32 在 1.7e9（2023年时间戳）
                           量级精度不足（只能精确到秒级整数），增量比对会失真。
        - title (VARCHAR): 文档标题。
        - heading (VARCHAR): 所属章节标题（媒体块为空）。
        - caption (VARCHAR): 表注/图注原文（媒体块用，文本块为空）。比 heading 宽，
          因为一条表注常是一整段话。
        - chunk_type (VARCHAR): 块类型 text | table | figure。
        - page_num_int (ARRAY INT64): 块覆盖到的页码（1 起）。用数组而非单值，
          因为一个章节可以跨页。
        - top_int (INT64): 块覆盖区域的最高点（y 最小值），供「按版面顺序回放」类需求。
        - position_int (ARRAY INT64): 块覆盖区域，**每 5 个一组**为
          ``(页, left, right, top, bottom)``。Milvus 只支持一层数组，故展平存放。
        - created_at (DOUBLE): 入库时间戳。与 mtime（文档修改时间）语义不同，
          两者都留：一个回答「这份文档什么时候变的」，一个回答「这条索引什么时候写的」。

        Args:
            dim: 向量维度。
        """
        schema = self._client.create_schema(auto_id=False)

        # ---- 字段定义 ----
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=128)
        schema.add_field("vector", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("text", DataType.VARCHAR, max_length=65535)
        schema.add_field("path", DataType.VARCHAR, max_length=1024)
        schema.add_field("mtime", DataType.DOUBLE)
        schema.add_field("title", DataType.VARCHAR, max_length=1024)
        schema.add_field("heading", DataType.VARCHAR, max_length=1024)
        schema.add_field("caption", DataType.VARCHAR, max_length=4096)
        schema.add_field("chunk_type", DataType.VARCHAR, max_length=16)
        schema.add_field("page_num_int", DataType.ARRAY,
                         element_type=DataType.INT64, max_capacity=_PAGE_CAPACITY)
        schema.add_field("top_int", DataType.INT64)
        schema.add_field("position_int", DataType.ARRAY,
                         element_type=DataType.INT64, max_capacity=_POSITION_CAPACITY)
        schema.add_field("created_at", DataType.DOUBLE)

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
            timeout=self._write_timeout,
        )

    def upsert(self, chunks: list[Chunk], embeddings, mtime: float = 0.0) -> None:
        """写入或覆盖一批块：同 id 的块覆盖旧数据（主键幂等）。

        关键设计：
        - ``text`` 字段存正文，因为查询默认返回块内容、BM25 重建也依赖它。
        - 使用 ``upsert`` 而非 ``insert``，实现按主键的幂等覆盖。
        - 写入后调用 ``flush()`` 强制落盘，使 ``count()`` 和 ``get_collection_stats``
          立刻反映新数据。若不 flush，统计数据可能滞后（只计已落盘段），
          导致索引一致性修复逻辑读到过期的 row_count。
        - 页码与最高点由块的 position 现算（不另存，避免两处数据不一致）。

        Args:
            chunks: Chunk 对象列表。
            embeddings: 对应的向量矩阵（numpy.ndarray 或 list），形状为 (len(chunks), dim)。
            mtime: 文档修改时间戳（浮点数），同一文档的所有块共享此值。
        """
        now = time.time()
        data = [
            {
                "id": c.id,
                "vector": embeddings[i].tolist(), # 转换为 Python list
                "text": c.text,
                "path": c.path,
                "mtime": float(mtime), # 确保为 float 类型
                "title": c.title,
                "heading": c.heading,
                "caption": c.caption,
                "chunk_type": c.chunk_type,
                "page_num_int": list(c.page_num),
                "top_int": c.top,
                # 展平位置：每 5 个一组 (页, left, right, top, bottom)
                "position_int": [v for pos in c.position for v in pos],
                "created_at": now,
            }
            for i, c in enumerate(chunks)
        ]
        self._client.upsert(collection_name=self._collection, data=data,
                            timeout=self._write_timeout)
        self._client.flush(self._collection, timeout=self._write_timeout)

    @staticmethod
    def _escape_filter_value(value: str) -> str:
        """转义 Milvus 过滤表达式里的字符串值：反斜杠与双引号。

        例如 "C:\\Users\\a.md" → "C:\\\\Users\\a.md"，否则破坏 filter 语法。
        从 delete_doc 抽出，供按值过滤的查询共用。

        Args:
            value: 待转义的过滤值（路径等字符串）。

        Returns:
            转义后可安全嵌入过滤表达式的字符串。
        """
        return value.replace("\\", "\\\\").replace('"', '\\"')

    def query(self, embedding, top_k: int) -> list[Chunk]:
        """按向量相似度检索，返回前 top_k 个块（带全部元数据）。

        元数据随搜索结果一并带回（output_fields），检索路径不需要再全表扫描补齐。
        不返回相似度：排序走名次（向量路名次 → RRF 融合 → 精排），相似度没有消费方；
        将来若要做「相似度下限」门槛，再把它加回来。

        Args:
            embedding: 查询向量，一维数组（numpy.ndarray 或 list）。
            top_k: 返回结果数量上限。

        Returns:
            list[Chunk]: 命中块，相似度由高到低；无命中时为空列表。
        """
        res = self._client.search(
            collection_name=self._collection,
            data=[embedding.tolist()], # 二维：[[v1, v2, ...]]
            limit=top_k,
            output_fields=list(_CHUNK_OUTPUT_FIELDS),
            timeout=self._read_timeout,
        )

        # Milvus 的 ``search`` 返回格式是嵌套结构：
        # - 外层列表：每个 query 向量对应一个元素（这里只有 1 个）。
        # - 内层列表：命中结果列表，每项包含 id、distance、entity 等字段。
        return [_to_chunk(h["entity"], h["id"]) for h in res[0]]

    def fetch_by_ids(self, ids: list[str]) -> list[Chunk]:
        """按块 id 批量定点取回块，供 BM25 命中补齐元数据。

        替代「全表扫描后按 id 过滤」：BM25 命中数 ≤ 30，定点 query 开销可忽略。
        不存在的 id 静默跳过；空列表直接返回空（不发请求）。

        Args:
            ids: 块 id 列表。

        Returns:
            list[Chunk]: 实际存在的块，顺序由 Milvus 返回顺序决定。
        """
        if not ids:
            return []
        quoted = ", ".join(f'"{self._escape_filter_value(i)}"' for i in ids)
        res = self._client.query(
            collection_name=self._collection,
            filter=f"id in [{quoted}]",
            output_fields=list(_CHUNK_OUTPUT_FIELDS),
            timeout=self._read_timeout,
        )
        return [_to_chunk(r) for r in res]

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
            timeout=self._read_timeout,
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
            timeout=self._write_timeout,
        )
        # 写入后调用 flush，使 count() 立即反映删除结果。
        self._client.flush(self._collection, timeout=self._write_timeout)

    def all_documents(self) -> list[tuple[Chunk, float]]:
        """返回全部块，每块为 (块, 文档修改时间)。

        为什么用 query_iterator 分页：
        - Milvus 单次 ``query`` 操作有 16384 行的返回上限（默认配置），
          超过限制会被静默截断，导致全量读取不完整。
        - ``query_iterator`` 自动分页，无此限制，可以遍历全部数据。

        使用场景：
        - BM25 索引重建：从向量库读取全部块（用它算索引文本）。
        - 索引状态重建：从元数据恢复 state 文件（用块路径与 mtime 做增量比对）。

        Returns:
            list[tuple[Chunk, float]]: 列表，每项为 (块, 文档修改时间)。
            **修改时间必须保持浮点数**——消费方按数值比对 mtime，改成对象会让
            它永远判定「已变更」，静默丢掉增量能力。
        """
        out: list[tuple[Chunk, float]] = []
        it = self._client.query_iterator(
            collection_name=self._collection,
            filter="", # 空过滤 = 全量
            output_fields=["text", "path", "title", "heading", "caption",
                           "chunk_type", "page_num_int", "position_int", "mtime"],
            batch_size=self._batch_size,
            timeout=self._read_timeout,
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
                    out.append((_to_chunk(row), float(row["mtime"])))
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
        stats = self._client.get_collection_stats(self._collection, timeout=self._read_timeout)
        return int(stats.get("row_count", 0))
