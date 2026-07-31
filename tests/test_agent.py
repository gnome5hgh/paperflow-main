# tests/test_agent.py
import pytest
from unittest.mock import MagicMock

from paperflow.core.tool import ToolResult
from paperflow.core.llm import Message, tool_to_openai_schema
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.agent import Agent


def make_mock_llm(responses: list[Message]):
    """Return a mock LLMClient whose chat() returns responses in sequence."""
    mock = MagicMock()
    async def chat(messages, tools=None, tool_choice="auto"):
        return responses.pop(0)
    mock.chat = chat
    mock.model = "mock"
    return mock


def make_mock_registry(tools, system_prompt="test prompt"):
    """Return a mock AgentRegistry for a test agent."""
    registry = MagicMock(spec=AgentRegistry)
    registry.get_config.return_value = AgentConfig(
        name="test",
        system_prompt=system_prompt,
        tools=tools,
    )
    return registry


class TestExecTool:
    def test_routes_to_correct_tool(self):
        from tests.conftest import MockEchoTool
        tool = MockEchoTool()
        registry = make_mock_registry([tool])
        llm = make_mock_llm([
            Message(role="assistant", content="Done.")
        ])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = agent._exec_tool({
            "id": "call_1",
            "function": {"name": "echo", "arguments": '{"message": "hello"}'},
        })
        assert result.text == "Echo: hello"

    def test_returns_error_on_json_decode_error(self):
        registry = make_mock_registry([])
        llm = make_mock_llm([])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = agent._exec_tool({
            "id": "call_1",
            "function": {"name": "any", "arguments": "{bad json"},
        })
        assert "Tool argument parse error" in result.text

    def test_returns_error_on_unknown_tool(self):
        registry = make_mock_registry([])
        llm = make_mock_llm([])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = agent._exec_tool({
            "id": "call_1",
            "function": {"name": "nonexistent", "arguments": "{}"},
        })
        assert "Unknown tool" in result.text


class TestAgentRun:
    @pytest.mark.asyncio
    async def test_returns_content_when_no_tool_calls(self):
        from tests.conftest import MockEchoTool
        registry = make_mock_registry([MockEchoTool()])
        llm = make_mock_llm([
            Message(role="assistant", content="Hello, I am the agent.")
        ])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = await agent.run("Hi!")
        assert result == "Hello, I am the agent."

    @pytest.mark.asyncio
    async def test_calls_tool_and_continues(self):
        from tests.conftest import MockEchoTool
        registry = make_mock_registry([MockEchoTool()])
        llm = make_mock_llm([
            Message(role="assistant", content=None, tool_calls=[{
                "id": "call_1",
                "type": "function",
                "function": {
                    "name": "echo",
                    "arguments": '{"message": "hello"}',
                },
            }]),
            Message(role="assistant", content="The tool said: Echo: hello"),
        ])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test")

        result = await agent.run("Echo hello!")
        assert result == "The tool said: Echo: hello"

    @pytest.mark.asyncio
    async def test_raises_max_turns_exceeded(self):
        from paperflow.core.agent import MaxTurnsExceeded
        registry = make_mock_registry([])
        # Always return a tool_call → agent can never finish
        llm = make_mock_llm([
            Message(role="assistant", content=None, tool_calls=[{
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "echo", "arguments": "{}"},
            }])
            for i in range(25)
        ])
        agent = Agent(llm=llm, agent_registry=registry, agent_type="test", max_turns=3)

        with pytest.raises(MaxTurnsExceeded):
            await agent.run("Loop forever!")
