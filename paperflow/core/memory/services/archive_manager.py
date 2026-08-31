"""ArchiveManager：可共享的 passage 集合（把长期记忆按主题归档）。

单用户场景下按需使用。archive 存 SQLite 的 archives 表：一行一个归档，
passage_ids 是该归档包含的 passage id 列表（JSON 序列化）。

注意：归档与 passage 是“弱引用”关系——归档只存储 passage 的 id 字符串，
不复制文本内容，也不在 passage 被软删时自动清理归档中的 id（调用方需自行维护）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.passage import Passage
from paperflow.core.memory.services.passage_manager import PassageManager

__all__ = ["Archive", "ArchiveManager"]


@dataclass
class Archive:
    """一个归档：id/name/description + 包含的 passage id 列表。

    特性：
        - passage_ids 仅存储 id 引用，不持有 Passage 对象副本，轻量且防数据冗余。
        - 删除 passage 时归档不会自动清理 id，由上层业务（如 Sleeptime）负责一致性。
    """

    id: str
    name: str
    description: str | None = None
    passage_ids: list[str] = field(default_factory=list)


class ArchiveManager:
    """归档业务层：建/列归档，并把 passage 加入归档（只记录 id，不复制内容）。"""

    def __init__(self, db: MemoryDB, passage_manager: PassageManager,
                 vector_db_provider: str = "NATIVE"):
        """初始化归档管理器并创建 archives 表（若不存在）。

        Args:
            db: 数据库连接。
            passage_manager: 文章管理器（当前仅用于依赖注入，预留扩展）。
            vector_db_provider: 向量数据库提供者标识（目前为接口占位，未实际使用）。

        表结构说明：
            - id: 归档唯一标识（主键）。
            - passage_ids: JSON 数组文本，存储该归档包含的所有 passage id。
        """
        self.db = db
        self.passage_manager = passage_manager
        # 幂等建表：passage_ids 以 TEXT 存储 JSON 字符串
        db.execute("CREATE TABLE IF NOT EXISTS archives ("
                   "id TEXT PRIMARY KEY, name TEXT, description TEXT,"
                   "passage_ids TEXT, created_at TEXT)")

    def create_archive(self, name: str, description: str | None = None) -> Archive:
        """新建归档（初始 passage_ids 为空列表）。

        Args:
            name: 归档名称（必填，用于展示和检索）。
            description: 归档描述（可选）。

        Returns:
            新创建的 Archive 对象（含自动生成的 id 和当前时间）。

        生成策略：
            - id 采用 "archive-" 前缀 + uuid hex，确保全局唯一且可读。
            - passage_ids 初始化为 "[]" 空 JSON 数组。
        """
        arch = Archive(id=f"archive-{uuid.uuid4().hex}", name=name,
                       description=description)
        self.db.execute(
            "INSERT INTO archives (id, name, description, passage_ids, created_at)"
            " VALUES (?,?,?,?, datetime('now'))",
            (arch.id, arch.name, arch.description, "[]"))
        return arch

    def list_archives(self) -> list[Archive]:
        """列出全部归档（passage_ids 从 JSON 还原）。

        Returns:
            Archive 对象列表，按数据库插入顺序（无特定排序）。

        反序列化细节：
            - 使用 json.loads 将 passage_ids 字符串还原为 Python list。
            - 若字段为 NULL 或空字符串，使用 `or []` 降级为空列表，避免 None 异常。
        """
        cur = self.db.execute("SELECT * FROM archives")
        out = []
        for r in cur.fetchall():
            row = dict(r)
            import json
            out.append(Archive(id=row["id"], name=row["name"],
                               description=row["description"],
                               passage_ids=json.loads(row["passage_ids"]) or []))
        return out

    def add_passage(self, archive_id: str, passage: Passage) -> Passage:
        """把 passage 加入归档：归档不存在抛 KeyError；只追加 id 不复制内容。

        Args:
            archive_id: 目标归档的 id。
            passage: 要加入的 Passage 对象（仅取其 id 字段存储）。

        Returns:
            原 Passage 对象（透传，便于链式调用）。

        Raises:
            KeyError: 当 archive_id 在数据库中不存在时。

        边界条件与注意事项：
            - 不检查 passage 是否已存在于该归档中——多次添加同一 passage 会导致
              passage_ids 中出现重复 id（去重由调用方负责）。
            - 不检查 passage 是否仍存在于 passage 表（软删后仍可被引用），
              这是“弱引用”设计，查询时由调用方按需过滤。
            - 使用 SELECT ... FOR UPDATE ？这里没有显式加锁，但在全局写锁
              （MemoryDB 的 execute 持有 threading.Lock）下，并发写是串行的，
              无需额外的悲观锁。
        """
        import json
        # 1. 查询归档是否存在（若不存在立即报错，避免后续无效更新）
        cur = self.db.execute("SELECT * FROM archives WHERE id=?", (archive_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"archive {archive_id} not found")

        # 2. 读取当前 passage_ids 列表，追加新 id 后写回
        ids = json.loads(row["passage_ids"]) or []
        ids.append(passage.id)
        self.db.execute("UPDATE archives SET passage_ids=? WHERE id=?",
                        (json.dumps(ids), archive_id))
        return passage