"""archival_passages 表操作。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.passage import Passage

__all__ = ["insert_passage", "select_passages", "select_passage_by_id",
           "soft_delete", "select_unique_tags", "count_passages"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_passage(db: MemoryDB, agent_id: str, p: Passage) -> None:
    db.execute(
        "INSERT INTO archival_passages (id, agent_id, text, embedding, tags,"
        " metadata_, is_deleted, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (p.id, agent_id, p.text,
         json.dumps(p.embedding) if p.embedding is not None else None,
         json.dumps(p.tags, ensure_ascii=False),
         json.dumps(p.metadata_, ensure_ascii=False), int(p.is_deleted),
         p.created_at.isoformat() if p.created_at else _now()))


def select_passages(db: MemoryDB, agent_id: str,
                    include_deleted: bool = False) -> list[dict]:
    sql = "SELECT * FROM archival_passages WHERE agent_id=?"
    params: list = [agent_id]
    if not include_deleted:
        sql += " AND is_deleted=0"
    cur = db.execute(sql + " ORDER BY created_at", tuple(params))
    return [dict(r) for r in cur.fetchall()]


def select_passage_by_id(db: MemoryDB, passage_id: str) -> dict | None:
    cur = db.execute("SELECT * FROM archival_passages WHERE id=?", (passage_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def soft_delete(db: MemoryDB, passage_id: str) -> None:
    db.execute("UPDATE archival_passages SET is_deleted=1 WHERE id=?", (passage_id,))


def select_unique_tags(db: MemoryDB, agent_id: str) -> list[str]:
    rows = select_passages(db, agent_id)
    tags: set[str] = set()
    for r in rows:
        tags.update(json.loads(r["tags"]) if r["tags"] else [])
    return sorted(tags)


def count_passages(db: MemoryDB, agent_id: str) -> int:
    cur = db.execute("SELECT COUNT(*) FROM archival_passages WHERE agent_id=? AND is_deleted=0",
                     (agent_id,))
    return cur.fetchone()[0]
