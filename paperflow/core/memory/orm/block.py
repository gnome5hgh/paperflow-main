"""blocks / block_history 表操作：块的读写与写前快照（撤销/重做历史）。

本文件只有裸 SQL 函数，不含业务规则；业务不变式（read_only / limit / 版本号
推进）由 services/block_manager.py 持有。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.block import Block

__all__ = ["insert_block", "select_block", "select_block_by_label", "select_blocks",
           "update_block", "delete_block", "checkpoint_block", "select_block_history",
           "restore_block_history"]


def _now() -> str:
    """当前 UTC 时间转 ISO 字符串（统一时间戳格式）。"""
    return datetime.now(timezone.utc).isoformat()


def insert_block(db: MemoryDB, b: Block, version: int = 1) -> None:
    """插入一个新块行；version 为初始版本号（默认 1），metadata_ 序列化为 JSON。"""
    db.execute(
        "INSERT INTO blocks (id, label, value, \"limit\", description, metadata_,"
        " read_only, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (b.id, b.label, b.value, b.limit, b.description,
         json.dumps(b.metadata_, ensure_ascii=False), int(b.read_only), version,
         _now(), _now()))


def select_block(db: MemoryDB, block_id: str) -> dict | None:
    """按 id 查块，返回 dict 行；不存在返回 None。"""
    cur = db.execute("SELECT * FROM blocks WHERE id=?", (block_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def select_block_by_label(db: MemoryDB, label: str) -> dict | None:
    """按 label 查块。label 无 UNIQUE 约束，这里取首个命中（调用方负责查重）。"""
    cur = db.execute("SELECT * FROM blocks WHERE label=?", (label,))
    row = cur.fetchone()
    return dict(row) if row else None


def select_blocks(db: MemoryDB) -> list[dict]:
    """返回全部块，按创建时间排序（保证 list_blocks 顺序稳定）。"""
    cur = db.execute("SELECT * FROM blocks ORDER BY created_at")
    return [dict(r) for r in cur.fetchall()]


def update_block(db: MemoryDB, block_id: str, value: str, version: int) -> None:
    """更新块的值与版本号并刷新 updated_at。"""
    db.execute("UPDATE blocks SET value=?, version=?, updated_at=? WHERE id=?",
               (value, version, _now(), block_id))


def delete_block(db: MemoryDB, block_id: str) -> None:
    """物理删除块行。"""
    db.execute("DELETE FROM blocks WHERE id=?", (block_id,))


def checkpoint_block(db: MemoryDB, block_id: str, label: str, value: str,
                     limit: int, description: str | None, metadata_: dict,
                     version: int) -> None:
    """写前快照：把块当前状态整体插入 block_history（撤销/重做的依据）。

    version 记录的是被快照那一刻的版本号，用于历史链排序与回滚定位。
    """
    db.execute(
        "INSERT INTO block_history (block_id, label, value, \"limit\", description,"
        " metadata_, version, checkpointed_at) VALUES (?,?,?,?,?,?,?,?)",
        (block_id, label, value, limit, description,
         json.dumps(metadata_, ensure_ascii=False), version, _now()))


def select_block_history(db: MemoryDB, block_id: str) -> list[dict]:
    """按 id 升序返回该块的全部历史快照（从旧到新）。"""
    cur = db.execute("SELECT * FROM block_history WHERE block_id=? ORDER BY id",
                     (block_id,))
    return [dict(r) for r in cur.fetchall()]


def restore_block_history(db: MemoryDB, history_id: int) -> dict:
    """取指定历史快照；id 不存在抛 KeyError（调用方据此报「回滚目标不存在」）。"""
    cur = db.execute("SELECT * FROM block_history WHERE id=?", (history_id,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"block_history id={history_id} not found")
    return dict(row)
