"""跨轮上下文累积：Agent.run 的 history 回放 / conv 累积 / 澄清不累积 / 无 compressor 回归。"""
import asyncio
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.llm import Message
from paperflow.core.memory.context_compressor import ContextCompressor
from paperflow.core.memory.context_config import ContextConfig, SummarySchema
from paperflow.core.session import Session
from tests.test_agent import make_capture_llm, make_mock_registry, MockIntentPipeline


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
                  intent_enabled=True, intent_pipeline=pipeline, session=Session(),
                  compressor=comp)
    result = asyncio.run(agent.run("搜索论文"))
    assert result == "你要搜索哪类论文？"
    assert comp.history == []        # 澄清早退不累积
    assert capture == []             # LLM 未被调用
