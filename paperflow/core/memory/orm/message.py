"""messages 表操作：对话消息的落盘与查询（Recall 的数据源）。

本文件只有裸 SQL 函数，不含业务规则；surrogate 清洗等语义由
services/message_manager.py 持有。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.message import Message, MessageRole

__all__ = ["insert_message", "select_messages_by_agent", "select_messages_by_ids",
           "search_messages", "count_messages"]


def _now() -> str:
    """当前 UTC 时间转 ISO 字符串（统一时间戳格式）。"""
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row) -> dict:
    """将 sqlite3.Row 转为普通 dict（便于 ORM 层处理）。

    Args:
        row: sqlite3.Row，一行查询结果（列名可访问）

    Returns:
        该行的普通 dict（按列名取值）。
    """
    return dict(row)


def insert_message(db: MemoryDB, agent_id: str, m: Message) -> None:
    """插入一条消息行：content 只存字符串（list/dict 内容序列化为 JSON），
    tool_calls 序列化为 JSON，created_at 缺省取当前时间。

    Args:
        db: 数据库连接。
        agent_id: 消息所属 agent 标识。
        m: 持久化消息对象（已含 id, role, content 等字段）。

    边界与注意：
        - content 字段在 schema 中为 TEXT，落盘时必须为字符串或 None。
          若 m.content 是 list 或 dict（如多模态内容），则序列化为 JSON 字符串存储；
          否则直接存储原字符串（或 None）。
        - tool_calls 始终序列化为 JSON 数组字符串（即使为空列表）。
        - created_at 若未显式设置，则使用当前 UTC 时间。
    """
    db.execute(
        "INSERT INTO messages (id, agent_id, role, content, tool_calls, tool_call_id,"
        " step_id, run_id, otid, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (m.id, agent_id, m.role.value,
         # 若 content 是 list/dict，转为 JSON 字符串；否则原样保留
         json.dumps(m.content, ensure_ascii=False)
         if isinstance(m.content, (list, dict)) else m.content,
         json.dumps(m.tool_calls, ensure_ascii=False),
         m.tool_call_id, m.step_id, m.run_id, m.otid,
         m.created_at.isoformat() if m.created_at else _now()))


def select_messages_by_agent(db: MemoryDB, agent_id: str,
                             limit: int | None = None) -> list[dict]:
    """按 agent 取消息，按 created_at 升序（同刻用 rowid 保插入序）；limit 可选。

    Args:
        db: 数据库连接。
        agent_id: 目标 agent 标识。
        limit: 可选返回条数上限，None 表示全部。

    Returns:
        消息行 dict 列表，按时间升序排列。

    排序细节：
        - 主排序 created_at（字符串 ISO 时间可字典序比较）。
        - 辅助排序 rowid（隐含的物理插入顺序）确保同一时刻消息顺序稳定。
    """
    sql = "SELECT * FROM messages WHERE agent_id=? ORDER BY created_at, rowid"
    params: list = [agent_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    cur = db.execute(sql, tuple(params))
    return [_row_to_dict(r) for r in cur.fetchall()]


def select_messages_by_ids(db: MemoryDB, ids: list[str]) -> list[dict]:
    """按 id 列表取消息，并保证返回顺序与输入 ids 一致。

    WHERE id IN (...) 返回行序任意；按输入 ids 建索引重排——压缩后按
    AgentState.message_ids 回放 in-context 窗口时对话顺序才正确。

    Args:
        db: 数据库连接。
        ids: 消息 id 列表（按期望顺序）。

    Returns:
        消息行 dict 列表，顺序与 ids 输入一致。

    注意：
        - 若 ids 为空，直接返回空列表，避免无效 SQL。
        - 使用字典 order 构建 id -> 索引映射，然后排序。
        - 若某个 id 不在表中，对应行缺失，排序时 `order.get(r["id"], 0)` 会将其放在开头，
          但这种情况通常不应发生（调用方应保证 ids 均有效）。
    """
    if not ids:
        return []
    placeholders = ",".join("?" for _ in ids)
    cur = db.execute(f"SELECT * FROM messages WHERE id IN ({placeholders})", tuple(ids))
    rows = [dict(r) for r in cur.fetchall()]
    # 构建 id 到索引位置的映射
    order = {i: idx for idx, i in enumerate(ids)}
    # 按映射排序，若 id 缺失则排在前面（理论上不会发生）
    rows.sort(key=lambda r: order.get(r["id"], 0))
    return rows


def search_messages(db: MemoryDB, agent_id: str, query: str,
                    roles: list[str] | None = None, limit: int = 5,
                    start_date: str | None = None,
                    end_date: str | None = None) -> list[dict]:
    """按 agent + 内容 LIKE 检索，支持 roles/时间区间过滤，结果按时间倒序限量。

    Args:
        db: 数据库连接。
        agent_id: agent 标识。
        query: 搜索关键词（自动包裹 % 进行 LIKE 匹配）。
        roles: 可选角色列表（如 ["user", "assistant"]），若提供则只返回这些角色的消息。
        limit: 返回条数上限（默认 5）。
        start_date: 起始时间（ISO 格式字符串），筛选 created_at >= start_date。
        end_date: 结束时间（ISO 格式字符串），筛选 created_at <= end_date。

    Returns:
        消息行 dict 列表，按 created_at 降序（最新优先），最多 limit 条。

    注意：
        - 所有条件均为 AND 关系。
        - roles 列表使用 IN 子句，若 roles 为空则不添加该条件。
        - 时间区间比较直接使用字符串字典序（ISO 格式有效）。
    """
    sql = "SELECT * FROM messages WHERE agent_id=? AND content LIKE ?"
    params: list = [agent_id, f"%{query}%"]

    # 角色过滤：若提供了 roles，生成 IN 子句
    if roles:
        sql += " AND role IN (%s)" % ",".join("?" for _ in roles)
        params.extend(roles)

    # 时间区间过滤
    if start_date:
        sql += " AND created_at >= ?"
        params.append(start_date)
    if end_date:
        sql += " AND created_at <= ?"
        params.append(end_date)

    # 排序与截断
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)

    cur = db.execute(sql, tuple(params))
    return [dict(r) for r in cur.fetchall()]


def count_messages(db: MemoryDB, agent_id: str) -> int:
    """返回该 agent 的消息总数（Sleeptime 游标与 size() 的数据源）。

    Args:
        db: 数据库连接。
        agent_id: agent 标识。

    Returns:
        消息总条数（包括所有历史消息，即使已被压缩移出窗口，因为物理行未删除）。
    """
    cur = db.execute("SELECT COUNT(*) FROM messages WHERE agent_id=?", (agent_id,))
    return cur.fetchone()[0]