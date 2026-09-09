"""PassageManager：archival memory（长期知识）持久化 + 语义检索。

embedder 复用 RAG 的千问嵌入模型；None 时退化为 tags/时间过滤检索（无语义）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.orm import passage as passage_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.passage import Passage

__all__ = ["PassageManager"]


def _row_to_schema(row: dict) -> Passage:
    """把 DB 行转回 Passage 模型（embedding/tags/metadata_ 从 JSON 还原）。

    Args:
        row: 数据库行（dict 形式，字段来自 archival_passages 表）。

    Returns:
        Passage 对象。

    注意：embedding 可能为 None（未生成向量），tags/metadata_ 为空时返回空容器。
    """
    return Passage(
        id=row["id"], text=row["text"],
        embedding=json.loads(row["embedding"]) if row["embedding"] else None,
        tags=json.loads(row["tags"]) if row["tags"] else [],
        metadata_=json.loads(row["metadata_"]) if row["metadata_"] else {},
        agent_id=row["agent_id"], is_deleted=bool(row["is_deleted"]),
        created_at=row["created_at"],
    )


def _ts(p: Passage) -> str:
    """created_at（datetime）统一转 ISO 字符串，与 DB 落盘格式一致，便于字符串比较。

    Args:
        p: Passage 对象。

    Returns:
        ISO 格式的时间字符串（如 "2026-08-31T10:00:00"），若 created_at 为 None 则返回空字符串。
    """
    return p.created_at.isoformat() if p.created_at else ""


class PassageManager:
    """archival 长期记忆业务层：写入带 embedding、检索按 tags/时间/语义过滤。"""

    def __init__(self, db: MemoryDB, embedder=None):
        """初始化 PassageManager。

        Args:
            db: 数据库连接。
            embedder: 可选的 embedding 模型（需实现 __call__(list[str]) -> np.ndarray）。
                若为 None，则检索退化为仅基于 tags 和时间过滤（无语义排序）。
        """
        self.db = db
        self.embedder = embedder

    def _embed(self, text: str) -> list[float] | None:
        """对文本生成 embedding；embedder 未注入时返回 None（退化为非语义检索）。

        Embedder 协议（见 paperflow.rag.encoders.embedder）是
        __call__(list[str]) -> np.ndarray，没有单独的 embed_query；取首行作为该
        文本的向量，pydantic 会把它归一成 list[float]。

        Args:
            text: 要编码的文本。

        Returns:
            浮点数列表（向量）或 None（若 embedder 不可用）。
        """
        if self.embedder is None:
            return None
        # embedder 接受列表并返回 numpy 数组，取第一个结果
        return self.embedder([text])[0]

    def insert_passage(self, agent_id: str, text: str,
                       tags: list[str] | None = None) -> Passage:
        """写入一条长期记忆（自动生成 embedding 与时间戳）。

        Args:
            agent_id: 所属 agent 标识。
            text: 文章/知识文本。
            tags: 标签列表（用于分类和过滤，可选）。

        Returns:
            新创建的 Passage 对象（含生成的 id、embedding 和 created_at）。

        注意：
            - embedding 在插入时同步生成（若 embedder 可用），避免后续单独更新。
            - created_at 自动取当前 UTC 时间。
        """
        p = Passage(text=text, tags=tags or [], embedding=self._embed(text),
                    agent_id=agent_id, created_at=datetime.now(timezone.utc))
        passage_orm.insert_passage(self.db, agent_id, p)
        return p

    def search_passages(self, agent_id: str, query: str,
                        tags: list[str] | None = None, top_k: int = 10,
                        start_datetime: str | None = None,
                        end_datetime: str | None = None) -> list[Passage]:
        """检索长期记忆：tags 全部命中 + 时间区间过滤，再按语义相似度降序取 top_k。

        有 embedder 且 query 非空时，把余弦相似度写进 p.metadata_["_score"] 排序
        （不污染 passage 本体字段）；embedder 缺失时按原始顺序截断返回。

        Args:
            agent_id: agent 标识。
            query: 查询文本（用于语义匹配，若 embedder 不可用则检索仅基于过滤条件）。
            tags: 可选标签列表，要求 passage 必须拥有所有给定标签（AND 逻辑）。
            top_k: 最大返回条数。
            start_datetime: 起始时间（ISO 字符串），过滤 created_at >= start_datetime。
            end_datetime: 结束时间（ISO 字符串），过滤 created_at <= end_datetime。

        Returns:
            Passage 列表，按相似度降序（若 embedder 可用），否则按原顺序（数据库插入顺序）。

        注意：
            - _score 字段仅作为临时排序依据，不会持久化到数据库。
            - 若 passage.embedding 为 None，则相似度计算跳过（视为 0 分）。
            - tags 过滤为“全匹配”策略，即 passage 必须包含所有指定标签（wanted <= set(p.tags)）。
        """
        # 1. 从数据库读取该 agent 的所有未软删 passage
        rows = passage_orm.select_passages(self.db, agent_id)
        passages = [_row_to_schema(r) for r in rows]

        # 2. 标签过滤（AND 逻辑）
        if tags:
            wanted = set(tags)
            passages = [p for p in passages if wanted <= set(p.tags)]

        # 3. 时间区间过滤
        if start_datetime:
            passages = [p for p in passages if _ts(p) >= start_datetime]
        if end_datetime:
            passages = [p for p in passages if _ts(p) <= end_datetime]

        # 4. 语义排序（若 embedder 可用且查询非空）
        if self.embedder is not None and query:
            # 生成查询向量
            qv = self.embedder([query])[0]
            # 为每个 passage 计算余弦相似度，存入 metadata_["_score"]
            for p in passages:
                if p.embedding:
                    p.metadata_["_score"] = _cosine(qv, p.embedding)
            # 按分数降序排序
            passages.sort(key=lambda p: p.metadata_.get("_score", 0.0), reverse=True)

        # 5. 截断返回 top_k
        return passages[:top_k]

    def delete_passage(self, passage_id: str) -> None:
        """软删一条长期记忆（保留行以便审计）。

        Args:
            passage_id: 要删除的 passage 标识。

        说明：软删仅将 is_deleted 置为 1，数据仍保留在表中，可恢复。
        """
        passage_orm.soft_delete(self.db, passage_id)

    def get_unique_tags(self, agent_id: str) -> list[str]:
        """返回该 agent 全部 passage 的去重标签列表。

        Args:
            agent_id: agent 标识。

        Returns:
            标签字符串列表，按字典序排序（已去重）。
        """
        return passage_orm.select_unique_tags(self.db, agent_id)

    def agent_passage_size(self, agent_id: str) -> int:
        """返回该 agent 未软删的 passage 数量。

        Args:
            agent_id: agent 标识。

        Returns:
            数量（不包括 is_deleted=1 的行）。
        """
        return passage_orm.count_passages(self.db, agent_id)


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度；任一侧零向量时归一因子取 1（避免除零）。

    Args:
        a: 向量 A（浮点数列表）。
        b: 向量 B（浮点数列表）。

    Returns:
        余弦相似度，范围 [-1, 1]。若任一向量全为 0，则返回 0。
        处理除零：当模长为 0 时视为 1，使结果为 0。
    """
    import math
    dot = sum(x * y for x, y in zip(a, b))
    # 计算两个向量的模长，若为 0 则用 1.0 替代（避免 ZeroDivisionError）
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)