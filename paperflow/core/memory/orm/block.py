"""blocks / block_history 表操作。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.block import Block

__all__ = ["insert_block", "select_block", "select_block_by_label", "select_blocks",
           "update_block", "delete_block", "checkpoint_block", "select_block_history",
           "restore_block_history"]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def insert_block(db: MemoryDB, b: Block, version: int = 1) -> None:
    db.execute(
        "INSERT INTO blocks (id, label, value, \"limit\", description, metadata_,"
        " read_only, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (b.id, b.label, b.value, b.limit, b.description,
         json.dumps(b.metadata_, ensure_ascii=False), int(b.read_only), version,
         _now(), _now()))


def select_block(db: MemoryDB, block_id: str) -> dict | None:
    cur = db.execute("SELECT * FROM blocks WHERE id=?", (block_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def select_block_by_label(db: MemoryDB, label: str) -> dict | None:
    cur = db.execute("SELECT * FROM blocks WHERE label=?", (label,))
    row = cur.fetchone()
    return dict(row) if row else None


def select_blocks(db: MemoryDB) -> list[dict]:
    cur = db.execute("SELECT * FROM blocks ORDER BY created_at")
    return [dict(r) for r in cur.fetchall()]


def update_block(db: MemoryDB, block_id: str, value: str, version: int) -> None:
    db.execute("UPDATE blocks SET value=?, version=?, updated_at=? WHERE id=?",
               (value, version, _now(), block_id))


def delete_block(db: MemoryDB, block_id: str) -> None:
    db.execute("DELETE FROM blocks WHERE id=?", (block_id,))


def checkpoint_block(db: MemoryDB, block_id: str, label: str, value: str,
                     limit: int, description: str | None, metadata_: dict,
                     version: int) -> None:
    db.execute(
        "INSERT INTO block_history (block_id, label, value, \"limit\", description,"
        " metadata_, version, checkpointed_at) VALUES (?,?,?,?,?,?,?,?)",
        (block_id, label, value, limit, description,
         json.dumps(metadata_, ensure_ascii=False), version, _now()))


def select_block_history(db: MemoryDB, block_id: str) -> list[dict]:
    cur = db.execute("SELECT * FROM block_history WHERE block_id=? ORDER BY id",
                     (block_id,))
    return [dict(r) for r in cur.fetchall()]


def restore_block_history(db: MemoryDB, history_id: int) -> dict:
    cur = db.execute("SELECT * FROM block_history WHERE id=?", (history_id,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"block_history id={history_id} not found")
    return dict(row)
