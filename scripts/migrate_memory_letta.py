"""一次性迁移：旧文件式记忆 → Letta SQLite 记忆（幂等，.migrated_letta 跳过）。

旧 *.md 保留为 MemFS 投影 → 建 SQL blocks；history.jsonl tool→messages、
reading→archival_passages；.cursor/.dream_cursor 丢弃；data/memory/.git 复用。
"""
from __future__ import annotations

import json
from pathlib import Path

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.orm import block as block_orm
from paperflow.core.memory.orm import message as message_orm
from paperflow.core.memory.orm import passage as passage_orm
from paperflow.core.memory.schemas.block import Block
from paperflow.core.memory.schemas.message import Message, MessageRole
from paperflow.core.memory.schemas.passage import Passage

_MARKER = ".migrated_letta"


def migrate(memory_dir: Path, db: MemoryDB, workspace: Path, agent_id: str = "sess_1") -> None:
    if (memory_dir / _MARKER).exists():
        return
    # 1. markdown 文件 → blocks（保留文件为投影，build .md → block）
    for path in sorted(memory_dir.glob("*.md")):
        if path.name in ("MEMORY.md", "memory_filesystem.md"):
            continue
        text = path.read_text(encoding="utf-8")
        label = path.stem
        value = _strip_frontmatter(text)
        block_orm.insert_block(db, Block.new(label, value), version=1)
    # 2. history.jsonl → messages / passages
    hist_path = memory_dir / "history.jsonl"
    if hist_path.exists():
        for line in hist_path.read_text(encoding="utf-8").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            # reading 是旧代码死分支(从未实际写);mark_read 是实际写入的「已读记录」,
            # 统一映射为 archival passage(tags=["reading"])——否则用户迁移后查不到旧记录
            if entry.get("type") in ("reading", "mark_read"):
                passage_orm.insert_passage(
                    db, agent_id, Passage(
                        text=str(entry.get("paper_title") or entry.get("path", "")),
                        tags=["reading"]))
            elif entry.get("type") == "tool":
                message_orm.insert_message(
                    db, agent_id, Message(role=MessageRole.tool,
                                          content=str(entry.get("tool_name", ""))))
    # 3. 迁移标记
    memory_dir.mkdir(parents=True, exist_ok=True)
    (memory_dir / _MARKER).write_text("1", encoding="utf-8")


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            return parts[2].strip("\n")
    return text


if __name__ == "__main__":
    from paperflow.config import PaperFlowConfig
    cfg = PaperFlowConfig.from_env()
    mem = Path(cfg.workspace) / "memory"
    migrate(mem, MemoryDB(Path(cfg.workspace) / "memory" / "memory.db"),
            Path(cfg.workspace))
    print("migration complete")
