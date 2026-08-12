# tests/memory/test_tool_manager.py
"""ToolManager 工具注册：标准记忆工具 + 清单工具挂载。"""
import tempfile
from pathlib import Path

from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.tool_manager import ToolManager


def test_list_tools_registered():
    from paperflow.core.memory.services.tool_manager import ToolManager
    tm = ToolManager(MemoryDB(Path(tempfile.mkdtemp()) / "m.db"))
    tm.upsert_base_tools()
    names = {t.name for t in tm.list_tools()}
    assert {"unread_list_add", "unread_list_remove", "history_append"} <= names
