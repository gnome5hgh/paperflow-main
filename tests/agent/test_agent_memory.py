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


@pytest.mark.asyncio
async def test_memory_self_edit_reflected_in_next_head():
    """评审 I-1：memory_replace 编辑块后，下一轮 _build_head 从 BlockManager 重建
    memory——会话内 self-edit 即进 system prompt，无需重启。"""
    from paperflow.core.memory.services.tool_manager import ToolManager
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    bm = BlockManager(db)
    mm = MessageManager(db)
    bm.create_block("persona", "旧身份")
    memory = Memory(blocks=[bm.get_block_by_label("persona")])
    tm = ToolManager(db)
    tm.bind(bm, None, mm, agent_id="sess_1")
    tm.upsert_base_tools()
    agent = Agent(llm=_FakeLLM(["完成"]), agent_registry=_Registry(), agent_type="_demo",
                  memory=memory, block_manager=bm, message_manager=mm,
                  memory_tools=tm.list_tools(), session_id="sess_1")
    # 会话内 LLM 调 memory_replace 改 persona 块
    res = tm.execute_tool("memory_replace", {
        "label": "persona", "old_string": "旧身份", "new_string": "新身份"}, "tc1")
    assert "Updated" in res.text
    head = await agent._build_head("task")
    sys_texts = "".join(m.content for m in head if m.role == "system")
    assert "新身份" in sys_texts
    assert "旧身份" not in sys_texts


def test_memory_self_edit_visible_in_same_run_next_turn():
    """评审 I-1：同一轮 ReAct 内 memory 工具编辑后，下一轮 LLM 调用的 head 即含新块。"""
    import asyncio
    from paperflow.core.memory.services.tool_manager import ToolManager
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    bm = BlockManager(db)
    mm = MessageManager(db)
    bm.create_block("persona", "旧身份")
    memory = Memory(blocks=[bm.get_block_by_label("persona")])
    tm = ToolManager(db)
    tm.bind(bm, None, mm, agent_id="sess_1")
    tm.upsert_base_tools()
    agent = Agent(llm=_FakeLLM(["完成"]), agent_registry=_Registry(), agent_type="_demo",
                  memory=memory, block_manager=bm, message_manager=mm,
                  memory_tools=tm.list_tools(), session_id="sess_1")
    head = asyncio.run(agent._build_head("task"))
    tm.execute_tool("memory_replace", {
        "label": "persona", "old_string": "旧身份", "new_string": "新身份"}, "tc1")
    agent._refresh_head_memory(head)   # 下一轮 LLM 调用前刷新
    sys_texts = "".join(m.content for m in head if m.role == "system")
    assert "新身份" in sys_texts
    assert "旧身份" not in sys_texts


@pytest.mark.asyncio
async def test_memory_index_injected_into_system():
    """评审 I-2：memory_filesystem.md 索引注入 system——非 system 块出现在索引树里
    （渐进暴露「按需加载」的文件树对 LLM 可见）。"""
    from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    bm = GitEnabledBlockManager(db, memfs_dir=tmp / "memory")
    mm = MessageManager(db)
    memory = Memory(blocks=[bm.create_block("persona", "身份")])
    bm.create_block("feedback_testing", "规则")     # 非 system 块 → 只进索引树
    agent = Agent(llm=_FakeLLM(["完成"]), agent_registry=_Registry(), agent_type="_demo",
                  memory=memory, block_manager=bm, message_manager=mm, session_id="sess_1")
    head = await agent._build_head("task")
    sys_texts = "".join(m.content for m in head if m.role == "system")
    assert "<memory_filesystem>" in sys_texts
    assert "feedback_testing.md" in sys_texts       # 非 system 块在索引树里
    assert "身份" in sys_texts                       # persona 内容仍常驻
