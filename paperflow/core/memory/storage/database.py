"""SQLite 连接与建表（sqlite3 标准库，零新增依赖）。

整库唯一连接单例：check_same_thread=False 允许多线程共享一条连接 + 一把
可重入锁串行化所有写事务——同轮多个并发子 agent 各自线程写记忆时不会互踩。
单条读写下锁后立即 commit；需要「读旧值 → 算新值 → 写回」的原子序列走
transaction()，整段持锁、只在最外层退出时提交。持久化只有「一张表一个主键、
一次写一条」的量级，裸 sqlite3 足够，不需要 ORM 层。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

__all__ = ["MemoryDB"]

# ============================================================================
# 数据库 Schema 定义（幂等建表）
# ============================================================================
_SCHEMA = """
-- blocks 表：核心记忆块存储（业务主表）
CREATE TABLE IF NOT EXISTS blocks (
    id TEXT PRIMARY KEY,                -- 块唯一标识
    label TEXT NOT NULL,                -- 业务标签（如 "assistant", "profile"）
    value TEXT NOT NULL,                -- 块内容（文本）
    "limit" INTEGER NOT NULL DEFAULT 2000, -- 字符长度上限
    description TEXT,                   -- 可读描述
    metadata_ TEXT,                     -- 扩展元数据（JSON 字符串）
    read_only INTEGER NOT NULL DEFAULT 0, -- 只读标志（0=可写，1=只读）
    version INTEGER NOT NULL DEFAULT 1,   -- 乐观锁版本号（单调递增）
    created_at TEXT NOT NULL,           -- 创建时间（ISO 字符串）
    updated_at TEXT NOT NULL            -- 最后更新时间（ISO 字符串）
);

-- block_history 表：块历史快照（撤销/重做链）
CREATE TABLE IF NOT EXISTS block_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT, -- 自增主键（排序依据）
    block_id TEXT NOT NULL,              -- 对应的块 ID
    label TEXT NOT NULL,                 -- 快照时的标签
    value TEXT NOT NULL,                 -- 快照时的内容
    "limit" INTEGER NOT NULL,            -- 快照时的字符上限
    description TEXT,                    -- 快照时的描述
    metadata_ TEXT,                      -- 快照时的元数据（JSON）
    version INTEGER NOT NULL,            -- 快照时的版本号
    checkpointed_at TEXT NOT NULL        -- 快照创建时间
);

-- messages 表：对话消息持久化（Recall 数据源）
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,                 -- 消息唯一标识
    agent_id TEXT NOT NULL,              -- 所属 agent（具体对话）
    session_id TEXT,                     -- 会话分组（同一会话的 supervisor 与子 agent）
    role TEXT NOT NULL,                  -- system/user/assistant/tool
    content TEXT,                        -- 消息内容（纯文本，可为 NULL）
    tool_calls TEXT,                     -- 工具调用列表（JSON 数组）
    tool_call_id TEXT,                   -- 工具调用 ID（关联 tool 消息）
    step_id TEXT,                        -- 工作流步骤标识
    run_id TEXT,                         -- 运行标识
    otid TEXT,                           -- 开放追踪 ID
    meta TEXT,                           -- 响应信封（JSON：结束原因/token 用量/模型快照/思维链等）
    created_at TEXT NOT NULL             -- 创建时间
);
-- 复合索引：加速按 agent + 时间查询
CREATE INDEX IF NOT EXISTS idx_messages_agent ON messages(agent_id, created_at);
"""

# 增量列迁移清单：(表, 列名, 列定义)。建表脚本只能建出新库——已有库不会因为
# _SCHEMA 多写一列就自动获得它，故给已有表**加列**时必须同时在这里登记一行，
# init_schema 会按它比对并补齐。messages 只增不改，历史行新列为 NULL。
_ADDITIVE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("messages", "meta", "meta TEXT"),
    ("messages", "session_id", "session_id TEXT"),
    ("agent_state", "task_anchor", "task_anchor TEXT"),
)


class MemoryDB:
    """SQLite 连接单例：建库建表 + 线程安全的写事务。

    设计决策：
        - 使用 check_same_thread=False 允许跨线程共享连接（多线程模型下，由应用层保证串行化）。
        - 用可重入锁保护 execute/executemany/transaction，确保同一时间只有一个写事务执行。
        - 非事务的单条操作立即 commit，保证持久化原子性；事务内的语句由最外层统一提交。
        - 使用 sqlite3.Row 工厂，使查询结果支持列名访问（dict(row) 或 row["col"]）。

    Attributes:
        path: Path，数据库文件路径
        _conn: sqlite3.Connection，整库唯一连接（check_same_thread=False，由锁串行化）
        _lock: threading.RLock，可重入锁；transaction() 内会重入 execute，普通锁会自锁死
        _tx_depth: int，事务嵌套深度；>0 时 execute 不自行提交，由最外层 transaction 统一提交
    """

    def __init__(self, path: Path):
        """建库：自动创建父目录，连接用 Row 工厂（orm 函数依赖列名取值）。

        Args:
            path: 数据库文件路径（若目录不存在则自动创建）。

        注意：连接建立后自动执行 init_schema() 建表（幂等）。
        """
        self.path = Path(path)
        # 确保数据库文件所在目录存在
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 创建连接，允许多线程共享（但需外部加锁串行化）
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        # 设置 Row 工厂，使查询结果可像字典一样访问
        self._conn.row_factory = sqlite3.Row
        # 可重入锁：transaction() 内部会继续调 execute()，普通锁会自锁死。
        self._lock = threading.RLock()
        #: 事务嵌套深度：>0 时 execute 不自行提交，由最外层 transaction 统一提交。
        self._tx_depth = 0
        self.init_schema()

    def init_schema(self) -> None:
        """建表（幂等）+ 给已有库补齐缺的增量列。

        注意：新增表/改列定义要同步更新 _SCHEMA；给**已有表**加列还要登记到
        _ADDITIVE_COLUMNS，否则只影响新库，老库缺列会在读写时报 no such column。
        """
        self._conn.executescript(_SCHEMA)
        self._migrate_additive_columns()
        # 会话索引必须在补列之后建：老库的 session_id 是后加的，建表脚本里直接
        # CREATE INDEX 会在补列前因 no such column 失败。
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_messages_session"
            " ON messages(session_id, created_at)")
        self._init_fts()
        self._conn.commit()

    def _init_fts(self) -> None:
        """建消息全文索引与同步触发器；FTS5 不可用时置 `fts_available=False`。

        优先 trigram（中文子串可命中），其次 unicode61；两者都不可用时检索层
        退回 SQL LIKE。虚表存一份正文副本，换来的好处是检索不需要额外依赖。
        """
        self.fts_available = False
        self.fts_tokenizer = None
        for tokenizer in ("trigram", "unicode61"):
            try:
                self._conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5("
                    "content, message_id UNINDEXED, session_id UNINDEXED,"
                    " agent_id UNINDEXED, role UNINDEXED, created_at UNINDEXED,"
                    f" tokenize='{tokenizer}')")
                self.fts_available = True
                self.fts_tokenizer = tokenizer
                break
            except sqlite3.OperationalError:
                continue
        if not self.fts_available:
            return
        self._conn.executescript("""
        CREATE TRIGGER IF NOT EXISTS messages_fts_ai AFTER INSERT ON messages BEGIN
          INSERT INTO messages_fts(rowid, content, message_id, session_id, agent_id, role, created_at)
          VALUES (new.rowid, new.content, new.id, COALESCE(new.session_id, new.agent_id), new.agent_id, new.role, new.created_at);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_ad AFTER DELETE ON messages BEGIN
          DELETE FROM messages_fts WHERE rowid = old.rowid;
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_au AFTER UPDATE ON messages BEGIN
          DELETE FROM messages_fts WHERE rowid = old.rowid;
          INSERT INTO messages_fts(rowid, content, message_id, session_id, agent_id, role, created_at)
          VALUES (new.rowid, new.content, new.id, COALESCE(new.session_id, new.agent_id), new.agent_id, new.role, new.created_at);
        END;
        """)
        # 回填旧行（幂等）：触发器只覆盖初始化之后的新增/更新。
        self._conn.execute(
            "INSERT INTO messages_fts(rowid, content, message_id, session_id,"
            " agent_id, role, created_at)"
            " SELECT rowid, content, id, COALESCE(session_id, agent_id),"
            " agent_id, role, created_at FROM messages"
            " WHERE rowid NOT IN (SELECT rowid FROM messages_fts)")

    def _migrate_additive_columns(self) -> None:
        """按 _ADDITIVE_COLUMNS 给已存在的表补列（幂等，仅供 init_schema 调用）。

        只做「加列」这一种变更：列已存在即跳过，可反复执行。表不存在（异常库文件）
        时跳过而非建表——建表是 executescript 的职责。历史行的新列留 NULL，由读取方
        按缺省值处理。
        """
        for table, column, ddl in _ADDITIVE_COLUMNS:
            existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if existing and column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    @contextmanager
    def transaction(self):
        """把多条语句包成一次持锁的原子操作（读-改-写序列必须走这里）。

        可重入：嵌套调用共享同一把锁与同一次提交，只有最外层退出时才 commit /
        rollback。内部调 execute/executemany 不会提前提交，因此整段序列对外不可见。

        Yields:
            底层 sqlite3 连接（供需要直接操作的调用方使用）。
        """
        with self._lock:
            self._tx_depth += 1
            try:
                yield self._conn
            except BaseException:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.rollback()
                raise
            else:
                self._tx_depth -= 1
                if self._tx_depth == 0:
                    self._conn.commit()

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """持锁执行单条 SQL；不在事务内时立即 commit（事务内由最外层统一提交）。

        Args:
            sql: SQL 语句（支持参数化占位符 ?）。
            params: 参数元组，默认为空。

        Returns:
            sqlite3.Cursor 对象，可用于 fetch 结果。

        注意：
            - 非事务调用立即 commit（读语句同样走此路径，加锁主要为简化并发模型）。
            - 若在 transaction() 内调用，则不提交，由最外层退出时统一 commit/rollback。
            - 若需批量操作，使用 executemany。
        """
        with self._lock:
            cur = self._conn.execute(sql, params)
            if self._tx_depth == 0:
                self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: list[tuple]) -> None:
        """持锁批量执行；不在事务内时立即 commit（事务内由最外层统一提交）。

        Args:
            sql: SQL 语句（参数化）。
            seq: 参数元组列表，每个元组对应一次执行。

        适用场景：批量插入多条记录，减少事务开销。
        注意：失败时整个批量操作会回滚（SQLite 默认在事务中执行 executemany）。
        """
        with self._lock:
            self._conn.executemany(sql, seq)
            if self._tx_depth == 0:
                self._conn.commit()