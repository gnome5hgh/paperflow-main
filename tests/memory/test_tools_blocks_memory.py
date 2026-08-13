"""blocks 复杂工具：memory 统一管理（create/replace/delete/rename）+ 简化 unified diff。"""
import tempfile
from pathlib import Path

import pytest

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.tools.blocks.memory import MemoryTool
from paperflow.core.memory.tools.blocks.memory_apply_patch import MemoryApplyPatchTool
from paperflow.core.memory.tools.runtime_context import MemoryToolsContext, set_memory_context


@pytest.fixture(autouse=True)
def _bm():
    db = MemoryDB(Path(tempfile.mkdtemp()) / "m.db")
    bm = BlockManager(db)
    set_memory_context(MemoryToolsContext(agent_id="sess_1", block_manager=bm))
    yield bm
    set_memory_context(None)


def test_delete_read_only_rejected(_bm):
    _bm.create_block("persona", "身份", read_only=True)
    res = MemoryTool().execute(action="delete", label="persona")
    assert "read-only" in res.text.lower()
    assert _bm.get_block_by_label("persona") is not None


def test_delete_removes_block(_bm):
    _bm.create_block("feedback_testing", "规则")
    res = MemoryTool().execute(action="delete", label="feedback_testing")
    assert "Deleted" in res.text
    assert _bm.get_block_by_label("feedback_testing") is None


def test_create_duplicate_label_errors(_bm):
    _bm.create_block("persona", "v1")
    res = MemoryTool().execute(action="create", label="persona", value="v2")
    assert "already exists" in res.text.lower()
    assert len([b for b in _bm.list_blocks() if b.label == "persona"]) == 1


def test_rename_preserves_metadata(_bm):
    _bm.create_block("persona", "身份", read_only=True, limit=500, description="desc")
    res = MemoryTool().execute(action="rename", label="persona", value="human")
    assert "human" in res.text
    b = _bm.get_block_by_label("human")
    assert b is not None and b.value == "身份"
    assert b.read_only is True and b.limit == 500 and b.description == "desc"
    assert _bm.get_block_by_label("persona") is None


def test_apply_patch_removes_and_adds_lines(_bm):
    _bm.create_block("feedback_testing", "line1\nline2\nline3")
    res = MemoryApplyPatchTool().execute(label="feedback_testing", patch="-line2\n+line2b")
    assert "Applied patch" in res.text
    assert _bm.get_block_by_label("feedback_testing").value == "line1\nline2b\nline3"


def test_apply_patch_with_context_and_hunks(_bm):
    _bm.create_block("feedback_testing", "line1\nline2\nline3\nline4")
    patch = "@@ -1,3 +1,3 @@\n line1\n-line2\n+line2b\n line3\n@@ -4,1 +4,1 @@\n-line4\n+line4b"
    MemoryApplyPatchTool().execute(label="feedback_testing", patch=patch)
    assert _bm.get_block_by_label("feedback_testing").value == "line1\nline2b\nline3\nline4b"


def test_apply_patch_missing_line_errors(_bm):
    _bm.create_block("feedback_testing", "line1\nline2")
    res = MemoryApplyPatchTool().execute(label="feedback_testing", patch="-nope\n+x")
    assert "error" in res.text.lower()
    assert _bm.get_block_by_label("feedback_testing").value == "line1\nline2"


def test_apply_patch_multi_block_rejected(_bm):
    _bm.create_block("feedback_testing", "v1")
    res = MemoryApplyPatchTool().execute(
        label="feedback_testing", patch="*** Update Block: feedback_testing\n*** Add Block: x\n")
    assert "not supported" in res.text
    assert _bm.get_block_by_label("feedback_testing").value == "v1"


def test_apply_patch_missing_block_errors(_bm):
    res = MemoryApplyPatchTool().execute(label="nope", patch="-x")
    assert "no block" in res.text
