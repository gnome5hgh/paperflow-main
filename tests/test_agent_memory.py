import pytest
from unittest.mock import AsyncMock, MagicMock
from paperflow.core.agent import Agent
from paperflow.core.llm import Message
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.memory.memory_index import MemoryIndex
from paperflow.core.tool import Tool, ToolResult


class MemTool(Tool):
    name = "mem_tool"
    description = "t"
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


def make_llm(responder):
    mock = MagicMock()
    async def chat(messages, tools=None, tool_choice="auto", **kw):
        return responder(messages)
    mock.chat = chat
    mock.context_window = 65536
    return mock


def make_agent(memory_index=None, compressor=None, llm=None, tools=None):
    tools = tools or [MemTool()]
    registry = MagicMock(spec=AgentRegistry)
    registry.get_config.return_value = AgentConfig(
        name="test", system_prompt="SKILL_PROMPT", tools=tools,
    )
    return Agent(
        llm=llm or make_llm(lambda m: Message(role="assistant", content="done")),
        agent_registry=registry,
        agent_type="test",
        memory_index=memory_index,
        compressor=compressor,
    )


class TestMemoryInjection:
    @pytest.mark.asyncio
    async def test_injects_memory_index_after_skill(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("- [User](user_role.md) — role\n")
        idx = MemoryIndex(tmp_path)
        seen = {}
        def responder(messages):
            seen["roles"] = [m.role for m in messages[:3]]
            seen["contents"] = [m.content for m in messages[:3]]
            return Message(role="assistant", content="ok")
        agent = make_agent(memory_index=idx, llm=make_llm(responder))
        await agent.run("hi")
        assert seen["roles"] == ["system", "system", "user"]
        assert "SKILL_PROMPT" in seen["contents"][0]
        assert "user_role" in seen["contents"][1]

    @pytest.mark.asyncio
    async def test_injects_summary_between_memory_and_user(self, tmp_path):
        (tmp_path / "MEMORY.md").write_text("idx\n")
        idx = MemoryIndex(tmp_path)
        compressor = MagicMock()
        compressor.summary = "SUMMARY_TEXT"
        # MagicMock 的 should_compress 默认返回 truthy，会误入压缩分支；
        # 本测试只验证消息顺序，显式关闭压缩
        compressor.should_compress.return_value = False
        seen = {}
        def responder(messages):
            seen["contents"] = [m.content for m in messages[:4]]
            return Message(role="assistant", content="ok")
        agent = make_agent(memory_index=idx, compressor=compressor,
                           llm=make_llm(responder))
        await agent.run("hi")
        assert seen["contents"][0] == "SKILL_PROMPT"
        assert seen["contents"][1] == "idx"
        assert seen["contents"][2] == "SUMMARY_TEXT"
        assert seen["contents"][3] == "hi"

    @pytest.mark.asyncio
    async def test_checks_compression_per_turn(self, tmp_path):
        compressor = MagicMock()
        compressor.summary = None
        # 第一轮压缩触发，第二轮不触发（否则压缩会重建回 2 条消息，
        # responder 永远看到 len<3 → tool_call 死循环直到 MaxTurnsExceeded）
        compressor.should_compress.side_effect = [True, False]
        # 真实 ContextCompressor.compress 是 async，MagicMock 不可 await → AsyncMock
        compressor.compress = AsyncMock(return_value=[
            Message(role="system", content="SKILL_PROMPT"),
            Message(role="user", content="q"),
        ])
        # LLM 第一轮返回 tool_call，第二轮返回最终回答（确保至少两轮，压缩检查在每轮发生）
        def responder(messages):
            if len(messages) < 3:
                return Message(role="assistant", content=None, tool_calls=[{
                    "id": "c1", "type": "function",
                    "function": {"name": "mem_tool", "arguments": "{}"},
                }])
            return Message(role="assistant", content="final")
        agent = make_agent(compressor=compressor, llm=make_llm(responder))
        await agent.run("hi")
        assert compressor.should_compress.call_count >= 1
