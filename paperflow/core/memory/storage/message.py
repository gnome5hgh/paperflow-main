"""messages 表操作：对话消息的落盘与查询（Recall 的数据源）。

本文件只有裸 SQL 函数，不含业务规则；surrogate 清洗等语义由
services/message_manager.py 持有。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from paperflow.core.common.constants import MessageRole
from paperflow.core.llm.domain.dto import response_meta_to_dict
from paperflow.core.memory.storage.database import MemoryDB
from paperflow.core.memory.schemas.stored_message import StoredMessage

__all__ = ["insert_message", "select_messages_by_agent", "select_messages_by_session",
           "select_messages_by_ids", "search_messages", "count_messages"]


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


def insert_message(db: MemoryDB, agent_id: str, m: StoredMessage) -> None:
    """插入一条消息行：content 只存字符串（list/dict 内容序列化为 JSON），
    tool_calls / meta 序列化为 JSON，created_at 缺省取当前时间。

    Args:
        db: 数据库连接。
        agent_id: 消息所属 agent 标识。
        m: 持久化消息对象（已含 id, role, content 等字段）。

    边界与注意：
        - content 字段在 schema 中为 TEXT，落盘时必须为字符串或 None。
          若 m.content 是 list 或 dict（如多模态内容），则序列化为 JSON 字符串存储；
          否则直接存储原字符串（或 None）。
        - tool_calls 始终序列化为 JSON 数组字符串（即使为空列表）。
        - meta 为 None（非 assistant 消息、或端点未回信封）时存 NULL——「没有信封」
          与「信封里字段为 None」在库里都是可区分的（后者是一行 JSON）。
        - created_at 若未显式设置，则使用当前 UTC 时间。
    """
    db.execute(
        "INSERT INTO messages (id, agent_id, session_id, role, content, tool_calls,"
        " tool_call_id, step_id, run_id, otid, meta, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (m.id, agent_id, m.session_id, m.role.value,
         # 若 content 是 list/dict，转为 JSON 字符串；否则原样保留
         json.dumps(m.content, ensure_ascii=False)
         if isinstance(m.content, (list, dict)) else m.content,
         json.dumps(m.tool_calls, ensure_ascii=False),
         m.tool_call_id, m.step_id, m.run_id, m.otid,
         json.dumps(response_meta_to_dict(m.meta), ensure_ascii=False)
         if m.meta is not None else None,
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


def select_messages_by_session(db: MemoryDB, session_id: str,
                               limit: int | None = None) -> list[dict]:
    """按会话分组取消息（覆盖 supervisor 与子 agent），按时间升序。

    Args:
        db: 数据库连接。
        session_id: 会话标识。
        limit: 可选返回条数上限。

    Returns:
        消息行 dict 列表，按 created_at、rowid 升序。
    """
    sql = ("SELECT * FROM messages WHERE COALESCE(session_id, agent_id)=?"
           " ORDER BY created_at, rowid")
    params: list = [session_id]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(r) for r in db.execute(sql, tuple(params)).fetchall()]


def _search_fts(db: MemoryDB, agent_id: str | None, query: str,
                roles: list[str] | None, limit: int,
                start_date: str | None, end_date: str | None,
                session_id: str | None) -> list[dict] | None:
    """用 FTS5 检索；索引不可用或查询语法不支持时返回 None（由调用方回退 LIKE）。

    Args:
        db: 数据库连接。
        agent_id: agent 标识（session_id 为空时使用）。
        query: 搜索关键词。
        roles: 角色过滤。
        limit: 返回条数上限。
        start_date: 起始时间。
        end_date: 结束时间。
        session_id: 会话标识。

    Returns:
        命中行；FTS 不可用时为 None。
    """
    if len(query) >= 3:
        # trigram 索引需要 >=3 字符才有意义；用带引号的短语走 MATCH，
        # 真正利用 FTS 索引。短查询回退 LIKE（trigram 无法建立 2 字符索引）。
        clauses = ["f.content MATCH ?"]
        params: list = [f'"{query.replace(chr(34), chr(34) * 2)}"']
    else:
        clauses = ["f.content LIKE ?"]
        params = [f"%{query}%"]
    if session_id is not None:
        clauses.append("f.session_id = ?")
        params.append(session_id)
    else:
        clauses.append("f.agent_id = ?")
        params.append(agent_id)
    if roles:
        clauses.append("f.role IN (%s)" % ",".join("?" for _ in roles))
        params.extend(roles)
    if start_date:
        clauses.append("m.created_at >= ?")
        params.append(start_date)
    if end_date:
        clauses.append("m.created_at <= ?")
        params.append(end_date)
    sql = ("SELECT m.* FROM messages_fts f JOIN messages m ON m.rowid=f.rowid"
           f" WHERE {' AND '.join(clauses)} ORDER BY m.created_at DESC LIMIT ?")
    params.append(limit)
    try:
        return [dict(r) for r in db.execute(sql, tuple(params)).fetchall()]
    except sqlite3.OperationalError:
        return None


def search_messages(db: MemoryDB, agent_id: str | None, query: str,
                    roles: list[str] | None = None, limit: int = 5,
                    start_date: str | None = None,
                    end_date: str | None = None,
                    session_id: str | None = None) -> list[dict]:
    """按 agent 或会话 + 内容 LIKE 检索，支持 roles/时间区间过滤。

    Args:
        db: 数据库连接。
        agent_id: agent 标识；session_id 为空时按它过滤。
        query: 搜索关键词（自动包裹 % 进行 LIKE 匹配）。
        roles: 可选角色列表（如 ["user", "assistant"]），若提供则只返回这些角色的消息。
        limit: 返回条数上限（默认 5）。
        start_date: 起始时间（ISO 格式字符串），筛选 created_at >= start_date。
        end_date: 结束时间（ISO 格式字符串），筛选 created_at <= end_date。
        session_id: 会话标识；提供时按会话分组过滤（旧行回退 agent_id）。

    Returns:
        消息行 dict 列表，按 created_at 降序（最新优先），最多 limit 条。

    注意：
        - 所有条件均为 AND 关系。
        - roles 列表使用 IN 子句，若 roles 为空则不添加该条件。
        - 时间区间比较直接使用字符串字典序（ISO 格式有效）。
    """
    if getattr(db, "fts_available", False):
        rows = _search_fts(db, agent_id, query, roles, limit,
                           start_date, end_date, session_id)
        if rows is not None:
            return rows
    if session_id is not None:
        sql = "SELECT * FROM messages WHERE COALESCE(session_id, agent_id)=? AND content LIKE ?"
        params: list = [session_id, f"%{query}%"]
    else:
        sql = "SELECT * FROM messages WHERE agent_id=? AND content LIKE ?"
        params = [agent_id, f"%{query}%"]

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
    """返回该 agent 的消息总数（MemoryConsolidator 游标与 size() 的数据源）。

    Args:
        db: 数据库连接。
        agent_id: agent 标识。

    Returns:
        消息总条数（包括所有历史消息，即使已被压缩移出窗口，因为物理行未删除）。
    """
    cur = db.execute("SELECT COUNT(*) FROM messages WHERE agent_id=?", (agent_id,))
    return cur.fetchone()[0]