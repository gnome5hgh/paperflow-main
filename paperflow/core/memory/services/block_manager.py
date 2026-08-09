"""BlockManager：记忆块 CRUD + 乐观锁 + checkpoint_block 快照（撤销/重做）。

对应 Letta services/block_manager.py。GitEnabledBlockManager（git 变体）在
Task 4（memfs）中叠加。
"""
from __future__ import annotations

from paperflow.core.memory.orm import block as block_orm
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.block import Block

__all__ = ["BlockManager"]

_READ_ONLY = "block is read-only"
_LIMIT = "Exceeds {limit} character limit"


class BlockManager:
    def __init__(self, db: MemoryDB):
        self.db = db

    def _to_schema(self, row: dict) -> Block:
        import json
        return Block(
            id=row["id"], label=row["label"], value=row["value"], limit=row["limit"],
            description=row["description"],
            metadata_=json.loads(row["metadata_"]) if row["metadata_"] else {},
            read_only=bool(row["read_only"]),
            version=row["version"],
        )

    def _db_history(self, label: str) -> list[dict]:
        """测试辅助：按 label 取该块的历史快照。"""
        row = block_orm.select_block_by_label(self.db, label)
        if row is None:
            return []
        return block_orm.select_block_history(self.db, row["id"])

    def create_block(self, label: str, value: str, limit: int = 2000,
                     description: str | None = None,
                     read_only: bool = False) -> Block:
        b = Block.new(label, value)
        b.limit = limit
        b.description = description
        b.read_only = read_only
        block_orm.insert_block(self.db, b, version=1)
        return b

    def get_block(self, block_id: str) -> Block:
        row = block_orm.select_block(self.db, block_id)
        if row is None:
            raise KeyError(f"block {block_id} not found")
        return self._to_schema(row)

    def get_block_by_label(self, label: str) -> Block | None:
        row = block_orm.select_block_by_label(self.db, label)
        return self._to_schema(row) if row else None

    def list_blocks(self) -> list[Block]:
        return [self._to_schema(r) for r in block_orm.select_blocks(self.db)]

    def update_block_value(self, label: str, value: str) -> Block:
        row = block_orm.select_block_by_label(self.db, label)
        if row is None:
            raise KeyError(f"block {label} not found")
        if row["read_only"]:
            raise ValueError(_READ_ONLY)
        if len(value) > row["limit"]:
            raise ValueError(_LIMIT.format(limit=row["limit"]))
        new_version = row["version"] + 1
        # checkpoint：改动前快照 → block_history（撤销/重做依据）
        block_orm.checkpoint_block(self.db, row["id"], row["label"], row["value"],
                                   row["limit"], row["description"],
                                   {}, row["version"])
        block_orm.update_block(self.db, row["id"], value, new_version)
        return self.get_block(row["id"])

    def delete_block(self, block_id: str) -> None:
        block_orm.delete_block(self.db, block_id)

    def checkpoint_block(self, block_id: str) -> None:
        b = self.get_block(block_id)
        block_orm.checkpoint_block(self.db, b.id, b.label, b.value, b.limit,
                                   b.description, b.metadata_, 0)

    def restore_block(self, block_history_id: int) -> Block:
        snap = block_orm.restore_block_history(self.db, block_history_id)
        block_orm.update_block(self.db, snap["block_id"], snap["value"], snap["version"])
        return self.get_block(snap["block_id"])
