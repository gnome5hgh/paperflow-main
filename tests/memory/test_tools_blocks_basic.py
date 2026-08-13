"""blocks 基础工具：replace 唯一子串 / insert 行号 / read_only 拒绝 / 未绑定降级。"""
import tempfile
from pathlib import Path

import pytest

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.tools.blocks.memory_replace import MemoryReplaceTool
from paperflow.core.memory.tools.blocks.memory_insert import MemoryInsertTool
from paperflow.core.memory.tools.blocks.memory_rethink import MemoryRethinkTool
from paperflow.core.memory.tools.runtime_context import MemoryToolsContext, set_memory_context


@pytest.fixture(autouse=True)
def _bm():
    db = MemoryDB(Path(tempfile.mkdtemp()) / "m.db")
    bm = BlockManager(db)
    set_memory_context(MemoryToolsContext(agent_id="sess_1", block_manager=bm))
    yield bm
    set_memory_context(None)


def test_replace_unique_substring(_bm):
    _bm.create_block("feedback_testing", "规则一\n规则二")
    res = MemoryReplaceTool().execute(
        label="feedback_testing", old_string="规则一", new_string="新规则")
    assert "新规则" in res.text
    assert "规则一" not in _bm.get_block_by_label("feedback_testing").value


def test_replace_multiple_match_errors(_bm):
    _bm.create_block("feedback_testing", "重复\n重复")
    res = MemoryReplaceTool().execute(label="feedback_testing", old_string="重复", new_string="x")
    assert "error" in res.text.lower() or "multiple" in res.text.lower()


def test_replace_read_only_rejected(_bm):
    _bm.create_block("persona", "身份", read_only=True)
    res = MemoryReplaceTool().execute(label="persona", old_string="身份", new_string="x")
    assert "read" in res.text.lower()


def test_insert_line(_bm):
    _bm.create_block("feedback_testing", "line1\nline2")
    MemoryInsertTool().execute(
        label="feedback_testing", new_string="插播", insert_line=0)
    assert _bm.get_block_by_label("feedback_testing").value.splitlines()[0] == "插播"


def test_rethink_rewrites_block(_bm):
    _bm.create_block("persona", "旧身份")
    res = MemoryRethinkTool().execute(label="persona", new_memory="新身份")
    assert "Rewrote" in res.text
    assert _bm.get_block_by_label("persona").value == "新身份"


def test_unbound_context_degrades():
    set_memory_context(None)
    res = MemoryReplaceTool().execute(label="x", old_string="a", new_string="b")
    assert "记忆服务未装配" in res.text
