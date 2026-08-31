"""archival_passages 表操作：长期记忆（passage）的读写与软删。

本文件只有裸 SQL 函数，不含业务规则；embedding 生成、语义排序等由
services/passage_manager.py 持有。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.passage import Passage

__all__ = ["insert_passage", "select_passages", "select_passage_by_id",
           "soft_delete", "select_unique_tags", "count_passages"]


def _now() -> str:
    """当前 UTC 时间转 ISO 字符串（统一时间戳格式）。

    Returns:
        ISO 8601 格式的 UTC 时间字符串，例如 "2026-08-31T10:00:00.123456"。
    """
    return datetime.now(timezone.utc).isoformat()


def insert_passage(db: MemoryDB, agent_id: str, p: Passage) -> None:
    """插入一条 passage 行：embedding/tags/metadata_ 序列化为 JSON，created_at 缺省取当前。

    Args:
        db: 数据库连接。
        agent_id: 所属 agent 标识（存储时冗余到表中）。
        p: Passage 对象（含 text, embedding, tags, metadata_, is_deleted 等）。

    注意：
        - embedding 若不为 None，则序列化为 JSON 数组字符串；若为 None 则存储 NULL。
        - tags 和 metadata_ 始终序列化为 JSON 字符串（空列表/空字典转为 "[]"/"{}"）。
        - is_deleted 转换为整数 0/1。
        - created_at 若未在 p 中设置，则自动取当前 UTC 时间。
    """
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
    """按 agent 取 passage；默认排除软删行，按创建时间升序。

    Args:
        db: 数据库连接。
        agent_id: 目标 agent 标识。
        include_deleted: 是否包含已软删除的行（默认 False，即仅返回未删除的）。

    Returns:
        行 dict 列表，按 created_at 升序排列（字典序，ISO 时间字符串可比较）。

    注意：
        - 若 include_deleted=False，则自动添加 AND is_deleted=0 过滤条件。
        - 返回的是原始 dict 行（含 JSON 字符串字段），调用方需自行解析。
    """
    sql = "SELECT * FROM archival_passages WHERE agent_id=?"
    params: list = [agent_id]
    if not include_deleted:
        sql += " AND is_deleted=0"
    cur = db.execute(sql + " ORDER BY created_at", tuple(params))
    return [dict(r) for r in cur.fetchall()]


def select_passage_by_id(db: MemoryDB, passage_id: str) -> dict | None:
    """按 id 查 passage，返回 dict 行；不存在返回 None。

    Args:
        db: 数据库连接。
        passage_id: passage 的唯一标识。

    Returns:
        若存在则返回行 dict，否则返回 None。
    """
    cur = db.execute("SELECT * FROM archival_passages WHERE id=?", (passage_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def soft_delete(db: MemoryDB, passage_id: str) -> None:
    """软删：把 is_deleted 置 1（保留行以便审计/恢复，不物理删除）。

    Args:
        db: 数据库连接。
        passage_id: 要软删除的 passage ID。

    注意：此操作不检查行是否存在，若 id 不存在则无影响（但通常调用方应保证存在）。
    """
    db.execute("UPDATE archival_passages SET is_deleted=1 WHERE id=?", (passage_id,))


def select_unique_tags(db: MemoryDB, agent_id: str) -> list[str]:
    """聚合该 agent 全部 passage 的 tags 去重后按字典序返回。

    Args:
        db: 数据库连接。
        agent_id: agent 标识。

    Returns:
        去重后的标签字符串列表，按字典序排序（仅包含未软删的 passage）。

    实现细节：
        - 使用 select_passages 默认排除软删行。
        - 遍历每行，解析 tags JSON 字段（若为空则跳过），将标签加入 set 去重。
        - 最终转为列表并排序。
    """
    rows = select_passages(db, agent_id)
    tags: set[str] = set()
    for r in rows:
        # 解析 tags 字段（可能为 NULL 或空字符串）
        tags.update(json.loads(r["tags"]) if r["tags"] else [])
    return sorted(tags)


def count_passages(db: MemoryDB, agent_id: str) -> int:
    """统计未软删的 passage 数量。

    Args:
        db: 数据库连接。
        agent_id: agent 标识。

    Returns:
        该 agent 下 is_deleted=0 的记录总数。
    """
    cur = db.execute("SELECT COUNT(*) FROM archival_passages WHERE agent_id=? AND is_deleted=0",
                     (agent_id,))
    return cur.fetchone()[0]