"""跨轮上下文累积：Agent.run 的 history 回放 / conv 累积 / 澄清不累积 / 无 compressor 回归。"""
import asyncio
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.llm import Message
from paperflow.core.memory.context_compressor import ContextCompressor
from paperflow.core.memory.context_config import ContextConfig, SummarySchema
from paperflow.core.conversation_state import ConversationState
from tests.agent.test_agent import make_capture_llm, make_mock_registry, MockIntentPipeline


def make_structured(result: SummarySchema | None = None):
    structured = MagicMock()

    async def extract(prompt, schema, fallback=None):
        if result is not None:
            return result
        return fallback()

    structured.extract = extract
    return structured


def full_summary() -> SummarySchema:
    return SummarySchema(
        task_overview="t", current_state="c",
        important_discoveries="d", next_steps="n", context_to_preserve="p",
    )


def make_compressor():
    return ContextCompressor(ContextConfig(), MagicMock(context_window=65536),
                             make_structured(full_summary()))


def test_second_run_replays_first_conversation():
    capture = []
    llm = make_capture_llm([
        Message(role="assistant", content="已找到 5 篇论文"),
        Message(role="assistant", content="第一篇是 GraphCL"),
    ], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test",
                  compressor=make_compressor())
    asyncio.run(agent.run("搜索异构图神经网络论文"))
    asyncio.run(agent.run("把第一篇整理成笔记"))
    contents = [m.content for m in capture[1]]              # 第二轮 LLM 收到的 messages
    assert "搜索异构图神经网络论文" in contents              # 第一轮 user 被回放
    assert "已找到 5 篇论文" in contents                     # 第一轮 assistant 被回放
    assert "把第一篇整理成笔记" in contents                  # 当前轮 user 在


def test_history_accumulates_conversation_only():
    capture = []
    comp = make_compressor()
    llm = make_capture_llm([Message(role="assistant", content="回答1")], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test",
                  compressor=comp)
    asyncio.run(agent.run("问题1"))
    assert [m.role for m in comp.history] == ["user", "assistant"]
    assert [m.content for m in comp.history] == ["问题1", "回答1"]


def test_no_compressor_messages_shape_unchanged():
    # 回归：不带 compressor 的 Agent（子 agent）行为与现状一致
    capture = []
    llm = make_capture_llm([Message(role="assistant", content="回答")], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test")
    asyncio.run(agent.run("问题"))
    assert [m.role for m in capture[0]] == ["system", "user"]   # 现状：SKILL + user


def test_clarification_early_return_not_accumulated():
    from paperflow.core.intent.intent_schema import IntentOutput, IntentStep, IntentType
    capture = []
    comp = make_compressor()
    pipeline = MockIntentPipeline(result=IntentOutput(
        intent_type=IntentType.SEARCH_PAPER, confidence=0.9,
        source=IntentStep.ROUTER, clarification="你要搜索哪类论文？"))
    llm = make_capture_llm([Message(role="assistant", content="不该被消费")], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test",
                  intent_enabled=True, intent_pipeline=pipeline, conversation=ConversationState(),
                  compressor=comp)
    result = asyncio.run(agent.run("搜索论文"))
    assert result == "你要搜索哪类论文？"
    assert comp.history == []        # 澄清早退不累积
    assert capture == []             # LLM 未被调用


def test_compress_rebuild_preserves_current_turn():
    capture = []
    # 小 context + 低触发阈值：history 预置超阈值 → 首轮 model call 前必触发压缩
    config = ContextConfig(context_size=400, trigger_ratio=0.5, reserve_ratio=0.2)
    comp = ContextCompressor(config, MagicMock(context_window=65536),
                             make_structured(full_summary()))
    comp.history = [Message(role="user", content=f"问题{i}") for i in range(30)]
    llm = make_capture_llm([Message(role="assistant", content="回答")], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test",
                  compressor=comp)
    asyncio.run(agent.run("当前问题"))
    sent = capture[0]
    contents = [m.content for m in sent]
    assert "当前问题" in contents                              # 当前轮 user 保留
    sys_texts = [m.content for m in sent if m.role == "system"]
    assert any(t != "test prompt" for t in sys_texts)          # 摘要消息已生成（非 SKILL）
    assert len(sent) < 32                                      # 旧 history 被压缩掉（30 条→tail）


def test_compressed_summary_persists_next_run():
    """Task 3 review 强制：压缩产物（history[0] 摘要消息）必须跨轮持久。

    旧 compress() 只写 self.summary 不进 history → 压缩产物跨轮不持久（run2 回放
    不到摘要）。Task 4 换 compress_history（原地改写 history）后必须验证：
    run1 压缩把摘要写进 history[0]，run2 回放 messages 仍含该摘要消息。
    """
    capture = []
    config = ContextConfig(context_size=400, trigger_ratio=0.5, reserve_ratio=0.2)
    comp = ContextCompressor(config, MagicMock(context_window=65536),
                             make_structured(full_summary()))
    # 预置超阈值历史 → run1 首轮 model call 前触发压缩 → history[0] 摘要消息
    comp.history = [Message(role="user", content=f"问题{i}") for i in range(30)]
    llm = make_capture_llm([
        Message(role="assistant", content="回答1"),   # run1 压缩后的响应
        Message(role="assistant", content="回答2"),   # run2
    ], capture)
    agent = Agent(llm=llm, agent_registry=make_mock_registry([]), agent_type="test",
                  compressor=comp)
    asyncio.run(agent.run("当前问题"))
    # run1 结束：compress_history 已把摘要写进 history[0]
    assert comp.history[0].role == "system"
    assert comp.history[0].content.startswith("[对话摘要]")
    # run2 回放 messages 含该摘要消息（压缩产物跨轮持久）
    asyncio.run(agent.run("后续问题"))
    assert any(m.role == "system" and m.content.startswith("[对话摘要]") for m in capture[1])
