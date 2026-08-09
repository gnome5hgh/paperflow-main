"""PassageManager：archival memory（长期知识）持久化 + 语义检索。

embedder 复用 RAG 的 bge；None 时退化为 tags/时间过滤检索（无语义）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.orm import passage as passage_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.passage import Passage

__all__ = ["PassageManager"]


def _row_to_schema(row: dict) -> Passage:
    return Passage(
        id=row["id"], text=row["text"],
        embedding=json.loads(row["embedding"]) if row["embedding"] else None,
        tags=json.loads(row["tags"]) if row["tags"] else [],
        metadata_=json.loads(row["metadata_"]) if row["metadata_"] else {},
        agent_id=row["agent_id"], is_deleted=bool(row["is_deleted"]),
        created_at=row["created_at"],
    )


def _ts(p: Passage) -> str:
    """created_at（datetime）统一转 ISO 字符串，与 DB 落盘格式一致，便于字符串比较。"""
    return p.created_at.isoformat() if p.created_at else ""


class PassageManager:
    def __init__(self, db: MemoryDB, embedder=None):
        self.db = db
        self.embedder = embedder

    def _embed(self, text: str) -> list[float] | None:
        if self.embedder is None:
            return None
        # 代码库 Embedder 协议（paperflow.rag.embedder）是 __call__(list[str]) -> np.ndarray，
        # 没有 embed_query；取首行作为该文本的向量，pydantic 会把它归一成 list[float]。
        return self.embedder([text])[0]

    def insert_passage(self, agent_id: str, text: str,
                       tags: list[str] | None = None) -> Passage:
        p = Passage(text=text, tags=tags or [], embedding=self._embed(text),
                    agent_id=agent_id, created_at=datetime.now(timezone.utc))
        passage_orm.insert_passage(self.db, agent_id, p)
        return p

    def search_passages(self, agent_id: str, query: str,
                        tags: list[str] | None = None, top_k: int = 10,
                        start_datetime: str | None = None,
                        end_datetime: str | None = None) -> list[Passage]:
        rows = passage_orm.select_passages(self.db, agent_id)
        passages = [_row_to_schema(r) for r in rows]
        if tags:
            wanted = set(tags)
            passages = [p for p in passages if wanted <= set(p.tags)]
        if start_datetime:
            passages = [p for p in passages if _ts(p) >= start_datetime]
        if end_datetime:
            passages = [p for p in passages if _ts(p) <= end_datetime]
        if self.embedder is not None and query:
            qv = self.embedder([query])[0]
            for p in passages:
                if p.embedding:
                    p.metadata_["_score"] = _cosine(qv, p.embedding)
            passages.sort(key=lambda p: p.metadata_.get("_score", 0.0), reverse=True)
        return passages[:top_k]

    def delete_passage(self, passage_id: str) -> None:
        passage_orm.soft_delete(self.db, passage_id)

    def get_unique_tags(self, agent_id: str) -> list[str]:
        return passage_orm.select_unique_tags(self.db, agent_id)

    def agent_passage_size(self, agent_id: str) -> int:
        return passage_orm.count_passages(self.db, agent_id)


def _cosine(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
