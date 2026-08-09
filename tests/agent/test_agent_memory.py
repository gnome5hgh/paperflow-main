"""Agent 记忆集成测试：Memory 挂载、消息落盘、compaction 触发、记忆工具注入。"""
import tempfile
from pathlib import Path

import pytest

from paperflow.core.agent import Agent
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import BlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.passage_manager import PassageManager
from paperflow.core.memory.services.tool_manager import ToolManager
from paperflow.core.memory.compaction import CompactionSettings
from paperflow.core.memory.schemas.memory import Memory


class _Registry:
    """极简注册表替身：返回固定 system_prompt 与空工具。"""
    def get_config(self, agent_type):
        class _Cfg:
            system_prompt = "SKILL: 你是助手"
            tools = []
        return _Cfg()


class _FakeLLM:
    context_window = 1000000
    def __init__(self, replies): self._replies = list(replies)
    async def chat(self, messages, tools=None, **kw):
        return self._replies.pop(0) if self._replies else _Reply("完成")


class _Reply:
    def __init__(self, content, tool_calls=None, truncated=False):
        self.content = content; self.tool_calls = tool_calls; self.truncated = truncated


def _agent(replies=("完成",)):
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    bm = BlockManager(db)
    mm = MessageManager(db)
    pm = PassageManager(db)
    tm = ToolManager(db)
    tm.bind(bm, pm, mm, agent_id="sess_1")
    tm.upsert_base_tools()
    memory = Memory(blocks=[bm.create_block("persona", "身份")])
    return Agent(llm=_FakeLLM(list(replies)), agent_registry=_Registry(),
                 agent_type="_demo", memory=memory, block_manager=bm,
                 message_manager=mm, passage_manager=pm,
                 memory_tools=tm.list_tools(),
                 compaction=CompactionSettings(), session_id="sess_1")


@pytest.mark.asyncio
async def test_build_head_contains_memory():
    from paperflow.core.llm import Message as WM
    agent = _agent()
    head = await agent._build_head("task")
    contents = [m.content for m in head]
    assert contents[0] == "SKILL: 你是助手"
    assert any("<memory_blocks>" in c for c in contents)
    assert contents[-1] == "task"          # 末尾 user task


def test_messages_property_returns_wire_dicts():
    from paperflow.core.llm import Message as WM
    agent = _agent()
    agent._append_to_messages([WM(role="user", content="hi")])
    msgs = agent.messages
    assert msgs[0] == {"role": "user", "content": "hi"}


def test_conversation_persisted_to_message_manager():
    from paperflow.core.llm import Message as WM
    agent = _agent(["需要工具", "完成"])
    # 手工模拟一轮：append 并落盘
    agent._append_to_messages([WM(role="user", content="q")])
    agent._persist_conversation([WM(role="user", content="q")])
    assert agent.message_manager.size("sess_1") == 1


def test_memory_tools_injected():
    agent = _agent()
    names = set(agent.tools.keys())
    assert "memory_replace" in names and "conversation_search" in names


def test_compaction_triggers_when_over_threshold():
    from paperflow.core.llm import Message as WM
    agent = _agent()
    agent.compaction = CompactionSettings(trigger_ratio=0.5, context_size=100)
    big = [WM(role="user", content="x" * 500)]
    assert agent._needs_compaction(big) is True
