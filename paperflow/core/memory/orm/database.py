"""SQLite 连接与建表（sqlite3 标准库，零新增依赖）。

整库唯一连接单例：check_same_thread=False 允许多线程共享一条连接 + 一把
threading.Lock 串行化所有写事务并立即 commit——同轮多个并发子 agent 各自
线程写记忆时不会互踩。持久化只有「一张表一个主键、一次写一条」的量级，
裸 sqlite3 足够，不需要 ORM 层。
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

__all__ = ["MemoryDB"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY, label TEXT NOT NULL, value TEXT NOT NULL,
    "limit" INTEGER NOT NULL DEFAULT 2000, description TEXT, metadata_ TEXT,
    read_only INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS block_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, block_id TEXT NOT NULL,
    label TEXT NOT NULL, value TEXT NOT NULL, "limit" INTEGER NOT NULL,
    description TEXT, metadata_ TEXT, version INTEGER NOT NULL,
    checkpointed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, role TEXT NOT NULL,
    content TEXT, tool_calls TEXT, tool_call_id TEXT, step_id TEXT,
    run_id TEXT, otid TEXT, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id, created_at);
CREATE TABLE IF NOT EXISTS archival_passages (
    id TEXT PRIMARY KEY, agent_id TEXT, text TEXT NOT NULL, embedding TEXT,
    tags TEXT, metadata_ TEXT, is_deleted INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


class MemoryDB:
    """SQLite 连接单例：建库建表 + 线程安全的写事务。"""

    def __init__(self, path: Path):
        """建库：自动创建父目录，连接用 Row 工厂（orm 函数依赖列名取值）。"""
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row   # orm 帮助函数依赖 dict(row)/row["col"]
        self._lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        """执行建表脚本（IF NOT EXISTS，幂等）。"""
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """持锁执行单条写/读 SQL 并立即 commit，保证每次写原子落盘。"""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        """持锁批量执行（同样立即 commit）。"""
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()
