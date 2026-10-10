"""blocks / block_history 表操作：块的读写与写前快照（撤销/重做历史）。

本文件只有裸 SQL 函数，不含业务规则；业务不变式（read_only / limit / 版本号
推进）由 services/block_manager.py 持有。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.common.errors import ConcurrentUpdateError
from paperflow.core.memory.storage.database import MemoryDB
from paperflow.core.memory.schemas.block import Block

__all__ = ["insert_block", "select_block", "select_block_by_label", "select_blocks",
           "update_block", "update_block_label", "delete_block", "checkpoint_block",
           "select_block_history", "restore_block_history"]


def _now() -> str:
    """当前 UTC 时间转 ISO 字符串（统一时间戳格式）。

    Returns:
        ISO 8601 格式的 UTC 时间字符串，例如 "2026-08-31T10:00:00.123456"。
    """
    return datetime.now(timezone.utc).isoformat()


def insert_block(db: MemoryDB, b: Block, version: int = 1) -> None:
    """插入一个新块行；version 为初始版本号（默认 1），metadata_ 序列化为 JSON。

    Args:
        db: 数据库连接。
        b: 要插入的 Block 对象（必须包含 id, label, value, limit, description,
           metadata_, read_only 等字段）。
        version: 初始版本号，默认为 1（由调用方决定，通常为 1）。

    注意：
        - metadata_ 被序列化为 JSON 字符串存储。
        - read_only 被转换为整数（SQLite 的布尔值用 0/1 表示）。
        - created_at 和 updated_at 自动取当前 UTC 时间。
    """
    db.execute(
        "INSERT INTO blocks (id, label, value, \"limit\", description, metadata_,"
        " read_only, version, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (b.id, b.label, b.value, b.limit, b.description,
         json.dumps(b.metadata_, ensure_ascii=False), int(b.read_only), version,
         _now(), _now()))


def select_block(db: MemoryDB, block_id: str) -> dict | None:
    """按 id 查块，返回 dict 行；不存在返回 None。

    Args:
        db: 数据库连接。
        block_id: 块的唯一标识。

    Returns:
        若存在则返回该行的 dict（含所有列），否则返回 None。
    """
    cur = db.execute("SELECT * FROM blocks WHERE id=?", (block_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def select_block_by_label(db: MemoryDB, label: str) -> dict | None:
    """按 label 查块。label 无 UNIQUE 约束，这里取首个命中（调用方负责查重）。

    Args:
        db: 数据库连接。
        label: 块的标签名称。

    Returns:
        第一个匹配 label 的行 dict，若不存在则返回 None。

    边界条件：
        - 若多个块具有相同 label（非预期情况），本函数只返回第一个（按 rowid 顺序）。
          调用方应保证 label 的唯一性。
    """
    cur = db.execute("SELECT * FROM blocks WHERE label=?", (label,))
    row = cur.fetchone()
    return dict(row) if row else None


def select_blocks(db: MemoryDB) -> list[dict]:
    """返回全部块，按创建时间排序（保证 list_blocks 顺序稳定）。

    Args:
        db: 数据库连接。

    Returns:
        所有块的行 dict 列表，按 created_at 升序排列。
    """
    cur = db.execute("SELECT * FROM blocks ORDER BY created_at")
    return [dict(r) for r in cur.fetchall()]


def update_block(db: MemoryDB, block_id: str, value: str,
                 expected_version: int) -> None:
    """按期望版本写入块值（CAS）：版本不匹配即抛冲突，绝不静默覆盖。

    Args:
        db: 数据库连接。
        block_id: 要更新的块 ID。
        value: 新的内容文本。
        expected_version: 调用方读到的版本号。写入成功后版本推进为 expected_version + 1。

    Raises:
        ConcurrentUpdateError: 行已被其他写者推进（影响行数为 0）。

    注意：
        - 该操作不检查 read_only 或 limit，也不记录历史快照；
          这些业务规则由 BlockManager 在调用前处理。
        - updated_at 自动设为当前 UTC 时间。
    """
    cur = db.execute(
        "UPDATE blocks SET value=?, version=?, updated_at=? WHERE id=? AND version=?",
        (value, expected_version + 1, _now(), block_id, expected_version))
    if cur.rowcount == 0:
        raise ConcurrentUpdateError(block_id)


def update_block_label(db: MemoryDB, block_id: str, label: str) -> None:
    """原地改写块 label（label 迁移专用）。

    只动 label，不触 value/version/updated_at——迁移是身份改写而非内容编辑，
    不进历史快照、不推进版本号。

    Args:
        db: 数据库连接。
        block_id: 要改名的块 ID。
        label: 新 label。
    """
    db.execute("UPDATE blocks SET label=? WHERE id=?", (label, block_id))


def delete_block(db: MemoryDB, block_id: str) -> None:
    """物理删除块行。

    Args:
        db: 数据库连接。
        block_id: 要删除的块 ID。

    注意：物理删除不可恢复，建议在业务层确保 read_only 校验和备份。
    """
    db.execute("DELETE FROM blocks WHERE id=?", (block_id,))


def checkpoint_block(db: MemoryDB, block_id: str, label: str, value: str,
                     limit: int, description: str | None, metadata_: dict,
                     version: int) -> None:
    """写前快照：把块当前状态整体插入 block_history（撤销/重做的依据）。

    version 记录的是被快照那一刻的版本号，用于历史链排序与回滚定位。

    Args:
        db: 数据库连接。
        block_id: 被快照的块 ID。
        label: 块的标签。
        value: 块的内容。
        limit: 字符上限。
        description: 块描述（可为 None）。
        metadata_: 元数据字典（将序列化为 JSON）。
        version: 被快照时的版本号（通常是更新前的旧版本号）。

    用途：
        - 更新块前自动调用，保存旧状态。
        - 也可通过 BlockManager.checkpoint_block 手动调用（此时 version 常为 0）。
    """
    db.execute(
        "INSERT INTO block_history (block_id, label, value, \"limit\", description,"
        " metadata_, version, checkpointed_at) VALUES (?,?,?,?,?,?,?,?)",
        (block_id, label, value, limit, description,
         json.dumps(metadata_, ensure_ascii=False), version, _now()))


def select_block_history(db: MemoryDB, block_id: str) -> list[dict]:
    """按 id 升序返回该块的全部历史快照（从旧到新）。

    Args:
        db: 数据库连接。
        block_id: 块 ID。

    Returns:
        历史快照行列表，按主键 id（自增）升序排列，即从最早到最近。
    """
    cur = db.execute("SELECT * FROM block_history WHERE block_id=? ORDER BY id",
                     (block_id,))
    return [dict(r) for r in cur.fetchall()]


def restore_block_history(db: MemoryDB, history_id: int) -> dict:
    """取指定历史快照；id 不存在抛 KeyError（调用方据此报「回滚目标不存在」）。

    Args:
        db: 数据库连接。
        history_id: block_history 表的主键 ID（自增）。

    Returns:
        该历史快照的行 dict。

    Raises:
        KeyError: 当 history_id 在表中不存在时。

    注意：该函数只取快照数据，实际回滚（写回 blocks 表）由 BlockManager.restore_block 执行。
    """
    cur = db.execute("SELECT * FROM block_history WHERE id=?", (history_id,))
    row = cur.fetchone()
    if row is None:
        raise KeyError(f"block_history id={history_id} not found")
    return dict(row)