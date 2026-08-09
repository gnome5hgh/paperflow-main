"""跨轮上下文：Agent.run 的 MessageManager 落盘+回放 / 澄清不落盘 / 无 message_manager 回归 / compaction。

旧 ContextCompressor 机制已被 Task 9/11 取代——跨轮回放改经 MessageManager(SQL)，
压缩改为 CompactionSettings + run_compaction(只改 in-context 窗口,不删 SQL)。
本文件按新机制重写,保持原测试意图(跨轮回放 / 累积 / 澄清不持久化 / 压缩重建)。
"""
import asyncio
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.llm import Message
from paperflow.core.memory.compaction import CompactionSettings
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.conversation_state import ConversationState
from tests.agent.test_agent import make_capture_llm, make_mock_registry, MockIntentPipeline


def make_structured(result=None):
    """mock StructuredOutput：extract 返回预设结果或 fallback()（compaction 摘要路径）。"""
    structured = MagicMock()

    async def extract(prompt, schema, fallback=None):
        if result is not None:
            return result
        return fallback()

    structured.extract = extract
    return structured


def _mem_agent(llm, mm, **kw):
    """装配 message_manager 的 Agent（跨轮回放经 SQL）。"""
    return Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test",
                 message_manager=mm, session_id="sess_1", **kw)


def _preload(mm, n: int):
    """预置 n 条 user 消息到 MessageManager（模拟超阈值历史）。"""
    for i in range(n):
        mm.add_message("sess_1", Message(role="user", content=f"问题{i}"))


def test_second_run_replays_first_conversation():
    db = MemoryDB(Path(tempfile.mkdtemp()) / "memory.db")
    mm = MessageManager(db)
    capture = []
    llm = make_capture_llm([
        Message(role="assistant", content="已找到 5 篇论文"),
        Message(role="assistant", content="第一篇是 GraphCL"),
    ], capture)
    agent = _mem_agent(llm, mm)
    asyncio.run(agent.run("搜索异构图神经网络论文"))
    asyncio.run(agent.run("把第一篇整理成笔记"))
    contents = [m.content for m in capture[1]]              # 第二轮 LLM 收到的 messages
    assert "搜索异构图神经网络论文" in contents              # 第一轮 user 被回放
    assert "已找到 5 篇论文" in contents                     # 第一轮 assistant 被回放
    assert "把第一篇整理成笔记" in contents                  # 当前轮 user 在


def test_conversation_persisted_to_recall():
    db = MemoryDB(Path(tempfile.mkdtemp()) / "memory.db")
    mm = MessageManager(db)
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="回答1")], capture)
    agent = _mem_agent(llm, mm)
    asyncio.run(agent.run("问题1"))
    msgs = mm.get_messages_by_agent_id("sess_1")
    assert [m.role.value for m in msgs] == ["user", "assistant"]
    assert [m.content for m in msgs] == ["问题1", "回答1"]


def test_no_message_manager_messages_shape_unchanged():
    # 回归：不带 message_manager 的 Agent（子 agent）行为与现状一致
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="回答")], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test")
    asyncio.run(agent.run("问题"))
    assert [m.role for m in capture[0]] == ["system", "user"]   # 现状：SKILL + user


def test_clarification_early_return_not_persisted():
    from paperflow.core.intent.intent_schema import IntentOutput, IntentStep, IntentType
    db = MemoryDB(Path(tempfile.mkdtemp()) / "memory.db")
    mm = MessageManager(db)
    capture = []
    pipeline = MockIntentPipeline(result=IntentOutput(
        intent_type=IntentType.SEARCH_PAPER, confidence=0.9,
        source=IntentStep.ROUTER, clarification="你要搜索哪类论文？"))
    llm = make_capture_llm([Message(role="assistant", content="不该被消费")], capture)
    agent = _mem_agent(llm, mm, intent_enabled=True, intent_pipeline=pipeline,
                       conversation=ConversationState())
    result = asyncio.run(agent.run("搜索论文"))
    assert result == "你要搜索哪类论文？"
    assert mm.size("sess_1") == 0        # 澄清早退不落盘
    assert capture == []                 # LLM 未被调用


def test_compaction_rebuild_preserves_current_turn():
    db = MemoryDB(Path(tempfile.mkdtemp()) / "memory.db")
    mm = MessageManager(db)
    _preload(mm, 30)                     # 超阈值历史 → 首轮 model call 前触发压缩
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="回答")], capture)
    agent = _mem_agent(llm, mm, compaction=CompactionSettings(
        trigger_ratio=0.5, context_size=100, reserve_ratio=0.2),
        structured=make_structured())
    asyncio.run(agent.run("当前问题"))
    sent = capture[0]
    contents = [m.content for m in sent]
    assert "当前问题" in contents                              # 当前轮 user 保留
    sys_texts = [m.content for m in sent if m.role == "system"]
    assert any("[对话摘要]" in t for t in sys_texts)           # 摘要消息已生成（非 SKILL）
    assert len(sent) < 32                                      # 旧 history 被压缩掉（30 条→tail）


def test_compaction_does_not_delete_sql():
    """压缩只改 in-context 窗口,不删 SQL——Recall 完整保留可追溯(brief 关键约束)。

    旧 ContextCompressor 把摘要写进 history(跨轮状态);新 compaction 只改 self._messages,
    跨轮上下文靠 SQL 全量回放——此处锁定"SQL 不删"与"落盘照常"。
    """
    db = MemoryDB(Path(tempfile.mkdtemp()) / "memory.db")
    mm = MessageManager(db)
    _preload(mm, 30)
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="回答")], capture)
    agent = _mem_agent(llm, mm, compaction=CompactionSettings(
        trigger_ratio=0.5, context_size=100, reserve_ratio=0.2),
        structured=make_structured())
    asyncio.run(agent.run("当前问题"))
    # 30 预置 + user task + 最终回答；压缩只截断 in-context，SQL 一行未删
    assert mm.size("sess_1") == 32


def test_run2_message_order_head_history_task():
    """评审 I-1：当前 user task 必须恒在末位（head 前段 + 回放历史 + 当前 task）。

    head 前段(system/memory/INTENT)在前、回放历史在中、当前 user task 在末——
    否则 LLM 会把回放历史里的旧任务误当当前任务（旧版「历史在前、当前任务在末」）。
    """
    from paperflow.core.memory.schemas.block import Block
    from paperflow.core.memory.schemas.memory import Memory
    db = MemoryDB(Path(tempfile.mkdtemp()) / "memory.db")
    mm = MessageManager(db)
    memory = Memory(blocks=[Block(label="persona", value="身份")])
    capture = []
    llm = make_capture_llm([
        Message(role="assistant", content="回答1"),
        Message(role="assistant", content="回答2"),
    ], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test",
                  memory=memory, message_manager=mm, session_id="sess_1")
    asyncio.run(agent.run("任务1"))
    asyncio.run(agent.run("任务2"))
    run2 = capture[1]                       # run2 的 LLM 输入
    roles = [m.role for m in run2]
    contents = [m.content for m in run2]
    # head(system/memory) 在前
    assert roles[0] == "system" and contents[0] == "test prompt"
    assert roles[1] == "system" and "<memory_blocks>" in contents[1]
    # 回放历史在中（run1 的 user + assistant）
    assert contents.index("任务1") < len(contents) - 1
    assert "回答1" in contents
    # 当前 user task 在末位
    assert roles[-1] == "user" and contents[-1] == "任务2"


def test_truncated_then_compaction_clears_accumulated():
    """评审 I-2：截断续写 + 压缩互斥——压缩分支必须清空累积器。

    半截(x*500) 让下一轮超阈值触发压缩;压缩可能驱逐半截 → 续写无参照即完整重答。
    若不清 accumulated,结果 = 半截 + 完整重答(重复);清空后只交付完整重答。
    """
    db = MemoryDB(Path(tempfile.mkdtemp()) / "memory.db")
    mm = MessageManager(db)
    capture = []
    llm = make_capture_llm([
        Message(role="assistant", content="x" * 500, truncated=True),
        Message(role="assistant", content="完整回答"),
    ], capture)
    agent = _mem_agent(llm, mm, compaction=CompactionSettings(
        trigger_ratio=0.5, context_size=100, reserve_ratio=0.2),
        structured=make_structured())
    result = asyncio.run(agent.run("任务"))
    assert result == "完整回答"                    # 半截未参与合并（已清空）
    assert "x" * 500 not in result                  # 无「半截 + 完整重答」重复
