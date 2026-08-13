"""get_memory_tools：惰性返回全部 13 个记忆工具（模块级单例）。"""
from paperflow.core.memory.tools import get_memory_tools
from paperflow.core.memory import constants

EXPECTED = (constants.BASE_MEMORY_TOOLS
            | {"archival_memory_insert", "archival_memory_search", "conversation_search",
               "extract_title"})


def test_returns_all_13_tools():
    tools = get_memory_tools()
    names = {t.name for t in tools}
    assert len(tools) == 13
    assert names == EXPECTED


def test_singleton_returns_same_instances():
    assert get_memory_tools() is get_memory_tools()
