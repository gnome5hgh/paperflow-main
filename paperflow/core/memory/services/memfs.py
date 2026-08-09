"""MemFS：记忆块的 git-backed markdown 投影层（Letta MemFS / context repository）。

SQL blocks 是源，markdown 是投影——双向同步：块变更写文件 + git commit；
文件被人工编辑后检测并回写块。自动索引 memory_filesystem.md（不可编辑）。
"""
from __future__ import annotations

import re
from pathlib import Path

from paperflow.core.memory.schemas.block import Block

__all__ = ["MemFS"]

_SYSTEM_LABELS = {"persona", "human"}
_INDEX_NAME = "memory_filesystem.md"


class MemFS:
    def __init__(self, memory_dir: Path, db=None):
        self.memory_dir = Path(memory_dir)
        self.system_dir = self.memory_dir / "system"
        self.db = db                       # MemoryDB | None（detect_file_changes 读 blocks 用）

    def _file_for(self, block: Block) -> Path:
        if block.label in _SYSTEM_LABELS:
            return self.system_dir / f"{block.label}.md"
        return self.memory_dir / f"{block.label}.md"

    def sync_block_to_file(self, block: Block) -> Path:
        """块 → markdown 投影（frontmatter 存 description/read_only/metadata）。"""
        path = self._file_for(block)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta_lines = [f"description: {block.description or ''}"]
        if block.read_only:
            meta_lines.append("read_only: true")
        front = "---\n" + "\n".join(meta_lines) + "\n---\n"
        path.write_text(front + block.value + "\n", encoding="utf-8")
        self.regenerate_index()
        return path

    def detect_file_changes(self) -> list[Block]:
        """扫描投影文件，值/描述与块不一致 → 返回需要回写的 Block 列表。"""
        if self.db is None:
            return []
        from paperflow.core.memory.orm import block as block_orm
        changed: list[Block] = []
        for row in block_orm.select_blocks(self.db):
            block = _row_to_block(row)
            path = self._file_for(block)
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            value = _strip_frontmatter(text)
            if value != block.value:
                block.value = value
                changed.append(block)
        return changed

    def regenerate_index(self) -> None:
        """重新生成 memory_filesystem.md（文件树 + 各文件 description）。"""
        lines = ["# Memory Filesystem（自动生成，请勿编辑）", ""]
        for path in sorted(self.memory_dir.rglob("*.md")):
            if path.name == _INDEX_NAME:
                continue
            rel = path.relative_to(self.memory_dir)
            desc = _frontmatter_field(path.read_text(encoding="utf-8"), "description")
            lines.append(f"- `{rel}`{(' — ' + desc) if desc else ''}")
        self.memory_dir.mkdir(parents=True, exist_ok=True)
        (self.memory_dir / _INDEX_NAME).write_text("\n".join(lines) + "\n",
                                                   encoding="utf-8")


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip("\n")
    return text


def _frontmatter_field(text: str, key: str) -> str:
    m = re.search(rf"^{key}:\s*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _row_to_block(row: dict) -> Block:
    import json
    return Block(id=row["id"], label=row["label"], value=row["value"],
                 limit=row["limit"], description=row["description"],
                 metadata_=json.loads(row["metadata_"]) if row["metadata_"] else {},
                 read_only=bool(row["read_only"]), version=row["version"])
