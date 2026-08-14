"""ArchiveManager：可共享的 passage 集合（把长期记忆按主题归档）。

单用户场景下按需使用。archive 存 SQLite 的 archives 表：一行一个归档，
passage_ids 是该归档包含的 passage id 列表（JSON 序列化）。
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
    """一个归档：id/name/description + 包含的 passage id 列表。"""

    id: str
    name: str
    description: str | None = None
    passage_ids: list[str] = field(default_factory=list)


class ArchiveManager:
    """归档业务层：建/列归档，并把 passage 加入归档（只记录 id，不复制内容）。"""

    def __init__(self, db: MemoryDB, passage_manager: PassageManager,
                 vector_db_provider: str = "NATIVE"):
        self.db = db
        self.passage_manager = passage_manager
        db.execute("CREATE TABLE IF NOT EXISTS archives ("
                   "id TEXT PRIMARY KEY, name TEXT, description TEXT,"
                   "passage_ids TEXT, created_at TEXT)")

    def create_archive(self, name: str, description: str | None = None) -> Archive:
        """新建归档（初始 passage_ids 为空列表）。"""
        arch = Archive(id=f"archive-{uuid.uuid4().hex}", name=name,
                       description=description)
        self.db.execute(
            "INSERT INTO archives (id, name, description, passage_ids, created_at)"
            " VALUES (?,?,?,?, datetime('now'))",
            (arch.id, arch.name, arch.description, "[]"))
        return arch

    def list_archives(self) -> list[Archive]:
        """列出全部归档（passage_ids 从 JSON 还原）。"""
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
        """把 passage 加入归档：归档不存在抛 KeyError；只追加 id 不复制内容。"""
        import json
        cur = self.db.execute("SELECT * FROM archives WHERE id=?", (archive_id,))
        row = cur.fetchone()
        if row is None:
            raise KeyError(f"archive {archive_id} not found")
        ids = json.loads(row["passage_ids"]) or []
        ids.append(passage.id)
        self.db.execute("UPDATE archives SET passage_ids=? WHERE id=?",
                        (json.dumps(ids), archive_id))
        return passage
