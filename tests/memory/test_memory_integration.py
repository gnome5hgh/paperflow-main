"""端到端记忆集成测试：对话落盘 + 记忆工具 + compaction 跨轮 + recall 全链路。

评审指出三个核心缺口（I-1 self-edit 不进 system / I-2 索引不注入 / I-3 压缩不
跨轮）正因缺端到端测试未早发现——本文件按 CLI 装配方式组装真实服务层
（GitEnabledBlockManager 让索引注入生效），跑完整 ReAct 循环验证全链路。
"""
import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from paperflow.core.agent import Agent
from paperflow.core.llm import Message
from paperflow.core.memory.compaction import CompactionSettings, SummarySchema
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.schemas.memory import Memory
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.passage_manager import PassageManager
from paperflow.core.memory.tools import get_memory_tools
from paperflow.core.memory.tools.runtime_context import (
    MemoryToolsContext, set_memory_context)
from tests.agent.test_agent import make_capture_llm, make_mock_registry


def _structured(result=None):
    """mock StructuredOutput（压缩摘要路径；result 非 None 时返回固定摘要）。"""
    structured = MagicMock()

    async def extract(prompt, schema, fallback=None):
        if result is not None:
            return result
        return fallback()
    structured.extract = extract
    return structured


def _tool_call(call_id: str, name: str, **args) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


def _services():
    """按 CLI 装配方式组装记忆服务层（GitEnabledBlockManager 让索引注入生效）。"""
    tmp = Path(tempfile.mkdtemp())
    db = MemoryDB(tmp / "memory.db")
    block_manager = GitEnabledBlockManager(db, memfs_dir=tmp / "memory")
    message_manager = MessageManager(db)
    passage_manager = PassageManager(db)
    agent_manager = AgentManager(db, block_manager, message_manager)
    message_manager.agent_manager = agent_manager   # 窗口追踪（评审 I-3）
    return (tmp, db, block_manager, message_manager, passage_manager,
            agent_manager)


def _preload(mm: MessageManager, n: int) -> None:
    for i in range(n):
        mm.add_message("sess_1", Message(role="user", content=f"问题{i}"))


def _mem_agent(llm, block_manager, message_manager, passage_manager,
               agent_manager, memory, *, structured=None, compaction=None):
    set_memory_context(MemoryToolsContext(
        agent_id="sess_1", block_manager=block_manager,
        passage_manager=passage_manager, message_manager=message_manager))
    return Agent(llm=llm, agent_registry=make_mock_registry(get_memory_tools()),
                 agent_type="test", memory=memory, block_manager=block_manager,
                 message_manager=message_manager, passage_manager=passage_manager,
                 agent_manager=agent_manager,
                 compaction=compaction, structured=structured, session_id="sess_1")


@pytest.fixture(autouse=True)
def _reset_ctx():
    yield
    set_memory_context(None)


def test_conversation_persists_and_recall_searches():
    """全链路：run 落盘 → Recall conversation_search 能查到全部对话。"""
    (tmp, db, bm, mm, pm, am) = _services()
    am.create_agent("sess_1")
    memory = Memory(blocks=[bm.create_block("persona", "身份")])
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="回答1"),
                            Message(role="assistant", content="回答2")], capture)
    agent = _mem_agent(llm, bm, mm, pm, am, memory)
    asyncio.run(agent.run("搜索 GraphCL"))
    asyncio.run(agent.run("整理笔记"))
    # 对话全量落盘 SQL（Recall）：两轮各 user+assistant
    assert mm.size("sess_1") == 4
    # conversation_search（Recall）检索到旧轮 user 输入
    hits = mm.search_messages("sess_1", "GraphCL")
    assert any("搜索 GraphCL" in h.content for h in hits)
    # AgentState.message_ids 追踪了全部 in-context 消息
    assert len(am.get_agent("sess_1").message_ids) == 4


def test_memory_tools_edit_block_and_archival_in_loop():
    """全链路：ReAct 循环内 memory_replace 改核心块、archival 写长期记忆；
    self-edit 后下一轮 system 即反映新块（I-1），索引注入 system（I-2）。"""
    (tmp, db, bm, mm, pm, am) = _services()
    am.create_agent("sess_1")
    bm.create_block("persona", "旧身份")
    memory = Memory(blocks=[bm.get_block_by_label("persona")])
    capture = []
    llm = make_capture_llm([
        Message(role="assistant", content="", tool_calls=[
            _tool_call("call_1", "memory_replace",
                       label="persona", old_string="旧身份", new_string="新身份")]),
        Message(role="assistant", content="", tool_calls=[
            _tool_call("call_2", "archival_memory_insert",
                       content="GraphCL 结论", tags=["reading"])]),
        Message(role="assistant", content="完成"),
    ], capture)
    agent = _mem_agent(llm, bm, mm, pm, am, memory)
    result = asyncio.run(agent.run("记住我的身份"))
    assert result == "完成"
    # memory_replace 经 agent 工具面执行：核心块已更新
    assert bm.get_block_by_label("persona").value == "新身份"
    # archival_memory_insert：长期记忆落盘
    assert pm.agent_passage_size("sess_1") == 1
    # 对话落盘完整：user + 两次 assistant(tool_calls) + 两次 tool 结果 + final
    assert mm.size("sess_1") == 6
    # self-edit 后下一轮 system prompt 反映新块（I-1，端到端经真实 run）
    head = asyncio.run(agent._build_head("新任务"))
    sys_texts = "".join(m.content for m in head if m.role == "system")
    assert "新身份" in sys_texts and "旧身份" not in sys_texts
    # 索引注入 system：memory_filesystem.md 文件树对 LLM 可见（I-2）
    assert "<memory_filesystem>" in sys_texts
    assert "persona.md" in sys_texts


def test_compaction_summary_across_runs_recall_complete():
    """全链路：超阈值历史触发压缩 → 摘要落盘跨轮回放；被驱逐旧消息不回放但
    SQL 全量保留（Recall 完整）。"""
    (tmp, db, bm, mm, pm, am) = _services()
    am.create_agent("sess_1")
    _preload(mm, 30)
    memory = Memory(blocks=[bm.create_block("persona", "身份")])
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="回答1"),
                            Message(role="assistant", content="回答2")], capture)
    agent = _mem_agent(
        llm, bm, mm, pm, am, memory,
        structured=_structured(result=SummarySchema(
            task_overview="T", current_state="S", important_discoveries="D",
            next_steps="N", context_to_preserve="C")),
        compaction=CompactionSettings(trigger_ratio=0.5, context_size=100,
                                      reserve_ratio=0.2))
    asyncio.run(agent.run("当前问题1"))
    asyncio.run(agent.run("当前问题2"))
    run2 = capture[1]
    contents = [m.content for m in run2]
    # 摘要跨轮可见（压缩产物持久化经 message_ids 回放）
    assert any(isinstance(c, str) and "[对话摘要]" in c for c in contents)
    # 被驱逐旧消息不回放（最早 preload 移出窗口）
    joined = "".join(str(c) for c in contents)
    assert "问题0" not in joined and "问题5" not in joined
    # 当前轮 user task 恒末位
    assert run2[-1].role == "user" and contents[-1] == "当前问题2"
    # SQL 保留全部原始消息（Recall 完整）+ 各轮摘要/落盘
    all_msgs = mm.get_messages_by_agent_id("sess_1")
    assert all(any(f"问题{i}" == m.content for m in all_msgs) for i in range(30))
    # conversation_search 仍能检索到被压缩驱逐的旧消息
    hits = mm.search_messages("sess_1", "问题0")
    assert any("问题0" in h.content for h in hits)
