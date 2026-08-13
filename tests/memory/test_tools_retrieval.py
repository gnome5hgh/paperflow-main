"""archival + recall：长期记忆写入/检索 + 对话检索 + 只读工具 risk=low。"""
import tempfile
from pathlib import Path

import pytest

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.passage_manager import PassageManager
from paperflow.core.memory.tools.archival.archival_memory_insert import ArchivalMemoryInsertTool
from paperflow.core.memory.tools.archival.archival_memory_search import ArchivalMemorySearchTool
from paperflow.core.memory.tools.recall.conversation_search import ConversationSearchTool
from paperflow.core.memory.tools.runtime_context import MemoryToolsContext, set_memory_context
from paperflow.core.llm import Message


@pytest.fixture(autouse=True)
def _ctx():
    db = MemoryDB(Path(tempfile.mkdtemp()) / "m.db")
    bm = BlockManager(db)
    pm = PassageManager(db)
    mm = MessageManager(db)
    set_memory_context(MemoryToolsContext(agent_id="sess_1", block_manager=bm,
                                          passage_manager=pm, message_manager=mm))
    yield pm, mm
    set_memory_context(None)


def test_risk_levels():
    assert ArchivalMemorySearchTool().risk_level == "low"
    assert ConversationSearchTool().risk_level == "low"
    assert ArchivalMemoryInsertTool().risk_level == "medium"


def test_archival_insert_and_search(_ctx):
    pm, _ = _ctx
    ArchivalMemoryInsertTool().execute(content="GraphCL 结论", tags=["reading"])
    assert pm.agent_passage_size("sess_1") == 1
    res = ArchivalMemorySearchTool().execute(query="", tags=["reading"])
    assert "GraphCL" in res.text


def test_conversation_search_default_filters_tool_msgs(_ctx):
    pm, mm = _ctx
    mm.add_message("sess_1", Message(role="user", content="搜索 GraphCL"))
    mm.add_message("sess_1", Message(role="tool", content="结果"))
    res = ConversationSearchTool().execute(query="GraphCL")
    assert "搜索 GraphCL" in res.text
    assert "结果" not in res.text    # 默认过滤 tool 消息（防递归）
