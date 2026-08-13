"""paper_lists：清单块通用操作 + unread/history 工具 + extract_title。"""
import re
import tempfile
from pathlib import Path

import pytest

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.tools.paper_lists.unread_list_add import UnreadListAddTool
from paperflow.core.memory.tools.paper_lists.unread_list_remove import UnreadListRemoveTool
from paperflow.core.memory.tools.paper_lists.history_append import HistoryAppendTool
from paperflow.core.memory.tools.paper_lists.extract_title import ExtractTitleTool
from paperflow.core.memory.tools.paper_lists import _common
from paperflow.core.memory.tools.runtime_context import MemoryToolsContext, set_memory_context
from paperflow.core.tool import ToolResult


@pytest.fixture(autouse=True)
def _bm():
    db = MemoryDB(Path(tempfile.mkdtemp()) / "m.db")
    bm = BlockManager(db)
    set_memory_context(MemoryToolsContext(agent_id="sess_1", block_manager=bm))
    yield bm
    set_memory_context(None)


def test_append_creates_block_and_updates(_bm):
    res = _common.append_line(_bm, "demo_list", "- A")
    assert "Appended" in res
    assert _bm.get_block_by_label("demo_list").value == "- A"


def test_append_is_append_only(_bm):
    _common.append_line(_bm, "demo_list", "- A")
    _common.append_line(_bm, "demo_list", "- B")
    assert _bm.get_block_by_label("demo_list").value == "- A\n- B"


def test_remove_line_by_key(_bm):
    _common.append_line(_bm, "demo_list", "- A")
    _common.append_line(_bm, "demo_list", "- B")
    res = _common.remove_line_by_key(_bm, "demo_list", "A")
    assert "Removed" in res
    assert _bm.get_block_by_label("demo_list").value == "- B"


def test_remove_missing_returns_error(_bm):
    _common.append_line(_bm, "demo_list", "- A")
    res = _common.remove_line_by_key(_bm, "demo_list", "不存在")
    assert "not found" in res


def test_remove_missing_block_does_not_create(_bm):
    res = _common.remove_line_by_key(_bm, "demo_list", "A")
    assert "not found" in res
    assert _bm.get_block_by_label("demo_list") is None


def test_unread_list_add_format(_bm):
    res = UnreadListAddTool().execute(title="某论文标题", source="arxiv:2301.001")
    assert "Appended" in res.text
    assert _bm.get_block_by_label("unread_list").value == "- 某论文标题 (arxiv:2301.001)"


def test_unread_list_add_rejects_missing_title():
    res = UnreadListAddTool().execute(source="arxiv:2301.001")
    assert "title" in res.text and "required" in res.text.lower()


def test_unread_list_remove_by_title(_bm):
    UnreadListAddTool().execute(title="论文A", source="pdf:/p/a.pdf")
    UnreadListAddTool().execute(title="论文B", source="pdf:/p/b.pdf")
    res = UnreadListRemoveTool().execute(title="论文A")
    assert "Removed" in res.text
    assert "论文A" not in _bm.get_block_by_label("unread_list").value
    assert "论文B" in _bm.get_block_by_label("unread_list").value


def test_history_append_format(_bm):
    res = HistoryAppendTool().execute(action="精读", title="论文A")
    assert "Appended" in res.text
    value = _bm.get_block_by_label("history_list").value
    assert re.match(r"^\[\d{4}-\d{2}-\d{2}", value)
    assert "精读" in value and "论文A" in value and "《" in value


def test_history_append_multiple_events_append_only(_bm):
    HistoryAppendTool().execute(action="精读", title="论文A")
    HistoryAppendTool().execute(action="写笔记", title="论文A")
    lines = _bm.get_block_by_label("history_list").value.splitlines()
    assert len(lines) == 2
    assert any("精读" in ln for ln in lines)
    assert any("写笔记" in ln for ln in lines)


def test_full_lifecycle(_bm):
    """加入 → 精读（history）→ 移除；history 留痕、unread 清空。"""
    UnreadListAddTool().execute(title="论文A", source="arxiv:2301.001")
    HistoryAppendTool().execute(action="精读", title="论文A")
    res = UnreadListRemoveTool().execute(title="论文A")
    assert "Removed" in res.text
    assert _bm.get_block_by_label("unread_list").value == ""
    assert "精读" in _bm.get_block_by_label("history_list").value


def test_ask_question_does_not_trigger_removal(_bm):
    UnreadListAddTool().execute(title="论文B", source="pdf:/p/b.pdf")
    assert "论文B" in _bm.get_block_by_label("unread_list").value
    assert _bm.get_block_by_label("history_list") is None


def test_history_dedup_query(_bm):
    for action in ("精读", "写笔记"):
        HistoryAppendTool().execute(action=action, title="论文A")
    HistoryAppendTool().execute(action="精读", title="论文B")
    lines = _bm.get_block_by_label("history_list").value.splitlines()
    titles = {ln[ln.find("《") + 1:ln.find("》")] for ln in lines}
    assert titles == {"论文A", "论文B"}


def test_extract_title_passthrough_when_given():
    res = ExtractTitleTool().execute(title="用户给的权威标题")
    assert res.text == "title: 用户给的权威标题\nsource: search"


def test_extract_title_no_extractor_errors():
    res = ExtractTitleTool().execute(pdf_path="/tmp/x.pdf")
    assert "extractor not available" in res.text
