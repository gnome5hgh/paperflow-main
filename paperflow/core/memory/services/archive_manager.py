"""ArchiveManager：可共享的 passage 集合（Letta services/archive_manager.py）。

paperFlow 单用户下按需使用，保留 Letta 接口。archive 存 SQLite（archives 表）。
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
    id: str
    name: str
    description: str | None = None
    passage_ids: list[str] = field(default_factory=list)


class ArchiveManager:
    def __init__(self, db: MemoryDB, passage_manager: PassageManager,
                 vector_db_provider: str = "NATIVE"):
        self.db = db
        self.passage_manager = passage_manager
        db.execute("CREATE TABLE IF NOT EXISTS archives ("
                   "id TEXT PRIMARY KEY, name TEXT, description TEXT,"
                   "passage_ids TEXT, created_at TEXT)")

    def create_archive(self, name: str, description: str | None = None) -> Archive:
        arch = Archive(id=f"archive-{uuid.uuid4().hex}", name=name,
                       description=description)
        self.db.execute(
            "INSERT INTO archives (id, name, description, passage_ids, created_at)"
            " VALUES (?,?,?,?, datetime('now'))",
            (arch.id, arch.name, arch.description, "[]"))
        return arch

    def list_archives(self) -> list[Archive]:
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
