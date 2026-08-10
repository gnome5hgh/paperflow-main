"""SQLite 连接与建表（sqlite3 标准库，零新增依赖）。

Letta 用 SQLAlchemy；paperFlow 保持轻量——单例连接 check_same_thread=False +
threading.Lock 包裹写事务（Layer 4 同一轮多 spawn 调用并发安全）。
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
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row   # orm 帮助函数依赖 dict(row)/row["col"]
        self._lock = threading.Lock()
        self.init_schema()

    def init_schema(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()
