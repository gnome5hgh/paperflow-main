"""messages 表操作。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.message import Message, MessageRole

__all__ = ["insert_message", "select_messages_by_agent", "select_messages_by_ids",
           "search_messages", "count_messages"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    return dict(row)


def insert_message(db: MemoryDB, agent_id: str, m: Message) -> None:
    db.execute(
        "INSERT INTO messages (id, agent_id, role, content, tool_calls, tool_call_id,"
        " step_id, run_id, otid, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (m.id, agent_id, m.role.value, json.dumps(m.content, ensure_ascii=False)
         if isinstance(m.content, (list, dict)) else m.content,
         json.dumps(m.tool_calls, ensure_ascii=False),
         m.tool_call_id, m.step_id, m.run_id, m.otid,
         m.created_at.isoformat() if m.created_at else _now()))


def select_messages_by_agent(db: MemoryDB, agent_id: str,
                             limit: int | None = None) -> list[dict]:
    sql = "SELECT * FROM messages WHERE agent_id=? ORDER BY created_at, rowid"
    params: list = [agent_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = db.execute(sql, tuple(params))
    return [_row_to_dict(r) for r in cur.fetchall()]


def select_messages_by_ids(db: MemoryDB, ids: list[str]) -> list[dict]:
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    cur = db.execute(f"SELECT * FROM messages WHERE id IN ({placeholders})", tuple(ids))
    rows = [dict(r) for r in cur.fetchall()]
    order = {i: idx for idx, i in enumerate(ids)}
    rows.sort(key=lambda r: order.get(r["id"], 0))
    return rows


def search_messages(db: MemoryDB, agent_id: str, query: str,
                    roles: list[str] | None = None, limit: int = 5,
                    start_date: str | None = None,
                    end_date: str | None = None) -> list[dict]:
    sql = "SELECT * FROM messages WHERE agent_id=? AND content LIKE ?"
    params: list = [agent_id, f"%{query}%"]
    if roles:
        sql += " AND role IN (%s)" % ",".join("?" for _ in roles)
        params.extend(roles)
    if start_date:
        sql += " AND created_at >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND created_at <= ?"
        params.append(end_date)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    cur = db.execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def count_messages(db: MemoryDB, agent_id: str) -> int:
    cur = db.execute("SELECT COUNT(*) FROM messages WHERE agent_id=?", (agent_id,))
    return cur.fetchone()[0]
