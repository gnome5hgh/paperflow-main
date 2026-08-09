"""迁移脚本测试：幂等 + 旧数据映射。"""
import json
import tempfile
from pathlib import Path

from scripts.migrate_memory_letta import migrate
from paperflow.core.memory.orm.database import MemoryDB


def _old_data(tmp: Path):
    mem = tmp / "memory"
    mem.mkdir(parents=True)
    (mem / "user_role.md").write_text(
        "---\ndescription: 用户角色\n---\n用户是研究生", encoding="utf-8")
    hist = []
    hist.append({"type": "tool", "tool_name": "read_pdf", "success": True,
                 "cursor": 1})
    hist.append({"type": "reading", "paper_title": "GraphCL", "cursor": 2})
    (mem / "history.jsonl").write_text(
        "\n".join(json.dumps(h) for h in hist) + "\n", encoding="utf-8")
    (mem / ".cursor").write_text("2", encoding="utf-8")
    return mem


def test_migrate_idempotent():
    tmp = Path(tempfile.mkdtemp())
    mem = _old_data(tmp)
    db = MemoryDB(tmp / "memory.db")
    migrate(mem, db, tmp)
    assert (mem / ".migrated_letta").exists()
    # blocks：user_role.md → block user_role
    from paperflow.core.memory.orm import block as block_orm
    row = block_orm.select_block_by_label(db, "user_role")
    assert row is not None and "研究生" in row["value"]
    # reading → passage
    from paperflow.core.memory.orm import passage as passage_orm
    assert len(passage_orm.select_passages(db, "sess_1")) == 1
    # 二次运行跳过（不重复建块）
    db2 = MemoryDB(tmp / "memory.db")
    migrate(mem, db2, tmp)
    assert len(block_orm.select_blocks(db2)) == 1
