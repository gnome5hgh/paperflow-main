# tests/memory/test_list_lifecycle.py
"""清单生命周期集成：加入→精读→history+询问→移除。"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from paperflow.core.memory.functions.function_sets.list_blocks import (
    UnreadListAddTool, UnreadListRemoveTool, HistoryAppendTool)
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.services.tool_manager import MemoryToolsContext


def _setup():
    db = MemoryDB(Path(tempfile.mkdtemp()) / "m.db")
    bm = BlockManager(db)
    return MemoryToolsContext(block_manager=bm), bm


def test_full_lifecycle_analyze_remove_flow():
    """加入 → 精读（history 追加）→ 确认移除。"""
    ctx, bm = _setup()
    UnreadListAddTool(ctx).execute(title="论文A", source="arxiv:2301.001")
    # 精读完成后 supervisor 的动作
    HistoryAppendTool(ctx).execute(action="精读", title="论文A")
    res = UnreadListRemoveTool(ctx).execute(title="论文A")
    assert "Removed" in res.text
    assert bm.get_block_by_label("unread_list").value == ""      # 已清空
    assert "精读" in bm.get_block_by_label("history_list").value  # history 留痕


def test_ask_question_does_not_trigger_removal():
    """ask_question 不追加 history、不移出未读——生命周期规则。"""
    ctx, bm = _setup()
    UnreadListAddTool(ctx).execute(title="论文B", source="pdf:/p/b.pdf")
    # ask_question 场景：什么都不做（不调 history_append / unread_list_remove）
    assert "论文B" in bm.get_block_by_label("unread_list").value
    assert bm.get_block_by_label("history_list") is None    # history 块未创建


def test_history_dedup_query():
    """「读过哪些」= history_list 去重聚合（模拟 qa-agent 查询视角）。"""
    ctx, bm = _setup()
    for action in ("精读", "写笔记"):
        HistoryAppendTool(ctx).execute(action=action, title="论文A")
    HistoryAppendTool(ctx).execute(action="精读", title="论文B")
    lines = bm.get_block_by_label("history_list").value.splitlines()
    titles = set()
    for ln in lines:
        start = ln.find("《") + 1
        end = ln.find("》")
        titles.add(ln[start:end])
    assert titles == {"论文A", "论文B"}     # 去重后读过的论文
