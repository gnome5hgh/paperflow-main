# tests/memory/test_list_blocks.py
"""ListBlockTool 基类：清单块通用操作（追加行/删行/缺失建块）。"""
import tempfile
from pathlib import Path

from paperflow.core.memory.functions.function_sets.list_blocks import ListBlockTool
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.services.tool_manager import MemoryToolsContext
from paperflow.core.tool import ToolResult


def _ctx(label="unread_list"):
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    bm = BlockManager(db)
    return MemoryToolsContext(block_manager=bm), bm


class _DemoTool(ListBlockTool):
    name = "demo_append"
    block_label = "demo_list"
    description = "demo"
    parameters = {"type": "object", "properties": {"title": {"type": "string"}},
                  "required": ["title"]}

    def _format_entry(self, title: str) -> str:
        return f"- {title}"


def test_append_creates_block_and_updates():
    ctx, bm = _ctx()
    t = _DemoTool(ctx)
    res = t.execute(title="A")
    assert isinstance(res, ToolResult)
    assert "Appended" in res.text
    assert bm.get_block_by_label("demo_list") is not None
    assert bm.get_block_by_label("demo_list").value == "- A"


def test_append_is_append_only():
    ctx, bm = _ctx()
    t = _DemoTool(ctx)
    t.execute(title="A")
    t.execute(title="B")
    # 只追加不改旧：第二次追加后仍含第一次的行
    assert bm.get_block_by_label("demo_list").value == "- A\n- B"


def test_remove_line_by_key():
    ctx, bm = _ctx()
    t = _DemoTool(ctx)
    t.execute(title="A")
    t.execute(title="B")
    res = t.execute(action="remove", title="A")
    assert "Removed" in res.text
    assert bm.get_block_by_label("demo_list").value == "- B"


def test_remove_missing_returns_error():
    ctx, bm = _ctx()
    t = _DemoTool(ctx)
    t.execute(title="A")
    res = t.execute(action="remove", title="不存在")
    assert "not found" in res.text
