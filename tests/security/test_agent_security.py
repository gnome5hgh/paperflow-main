import pytest
from unittest.mock import MagicMock
from paperflow.core.agent import Agent
from paperflow.core.security import (
    SecurityMiddleware, ConfirmRequired, PolicyDenied, SecurityBlocked,
)
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.llm import Message
from paperflow.core.agent_registry import AgentConfig, AgentRegistry


class CaptureTool(Tool):
    name = "capture"
    description = "records calls"
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "format": "content"},
        },
        "required": ["content"],
    }

    def execute(self, content: str) -> ToolResult:
        return ToolResult(text=f"got:{content}")


def make_llm(responder):
    mock = MagicMock()
    async def chat(messages, tools=None, tool_choice="auto", telemetry_callback=None):
        return responder(messages)
    mock.chat = chat
    return mock


def make_agent(middleware=None, confirm=None, tools=None, llm=None):
    tools = tools or [CaptureTool()]
    registry = MagicMock(spec=AgentRegistry)
    registry.get_config.return_value = AgentConfig(
        name="test", system_prompt="p", tools=tools,
    )
    return Agent(
        llm=llm or make_llm(lambda m: Message(role="assistant", content="done")),
        agent_registry=registry,
        agent_type="test",
        security_middleware=middleware or [],
        confirm_callback=confirm,
    )


def tool_call_msg(name, args):
    return Message(role="assistant", content=None, tool_calls=[{
        "id": "call_1", "type": "function",
        "function": {"name": name, "arguments": args},
    }])


class TestAgentMiddleware:
    @pytest.mark.asyncio
    async def test_before_deny_returns_toolresult_with_summary(self):
        class DenyMW(SecurityMiddleware):
            async def before(self, ctx):
                raise PolicyDenied("blocked by test")

        seen = []
        class AuditMW(SecurityMiddleware):
            async def after(self, ctx):
                seen.append(ctx.error)

        agent = make_agent(middleware=[DenyMW(), AuditMW()])
        result = await agent._exec_tool({
            "id": "c1", "function": {"name": "capture", "arguments": '{"content": "x"}'},
        })
        assert "policy_denied" in result.text
        assert result.summary["decision"] == "policy_denied"
        assert seen  # after hooks ran with ctx.error set

    @pytest.mark.asyncio
    async def test_confirm_denied_by_default(self):
        class ConfirmMW(SecurityMiddleware):
            async def before(self, ctx):
                raise ConfirmRequired("capture", {}, "medium", ["write_file"])

        agent = make_agent(middleware=[ConfirmMW()])
        result = await agent._exec_tool({
            "id": "c1", "function": {"name": "capture", "arguments": '{"content": "x"}'},
        })
        assert "User denied" in result.text
        # 默认 fail-safe 拒绝（无人工回调）→ auto_denied，与 ctx.approval_outcome 一致
        assert result.summary["decision"] == "auto_denied"

    @pytest.mark.asyncio
    async def test_confirm_accepted_executes_tool(self):
        class ConfirmMW(SecurityMiddleware):
            async def before(self, ctx):
                raise ConfirmRequired("capture", {}, "medium", ["write_file"])

        accepted = []
        async def confirm_cb(cr):
            accepted.append(cr.tool_name)
            return True

        agent = make_agent(middleware=[ConfirmMW()], confirm=confirm_cb)
        result = await agent._exec_tool({
            "id": "c1", "function": {"name": "capture", "arguments": '{"content": "hi"}'},
        })
        assert result.text == "got:hi"
        assert accepted == ["capture"]

    @pytest.mark.asyncio
    async def test_tool_result_type_normalization(self):
        class StrTool(Tool):
            name = "strtool"
            description = "returns plain str"
            parameters = {"type": "object", "properties": {}}

            def execute(self, **kwargs):
                return "plain string"

        agent = make_agent(tools=[StrTool()])
        result = await agent._exec_tool({
            "id": "c1", "function": {"name": "strtool", "arguments": "{}"},
        })
        assert result.text == "plain string"


class TestAgentOnFinish:
    @pytest.mark.asyncio
    async def test_on_finish_runs_on_content(self):
        class TransformMW(SecurityMiddleware):
            async def on_finish(self, agent, content):
                return content + "!"

        llm = make_llm(lambda m: Message(role="assistant", content="hello"))
        agent = make_agent(middleware=[TransformMW()], llm=llm)
        result = await agent.run("hi")
        assert result == "hello!"

    @pytest.mark.asyncio
    async def test_trace_id_set_per_run(self):
        llm = make_llm(lambda m: Message(role="assistant", content="ok"))
        agent = make_agent(llm=llm)
        await agent.run("a")
        tid1 = agent._trace_id
        await agent.run("b")
        tid2 = agent._trace_id
        assert tid1 != tid2
        assert tid1.startswith("trace_")

    @pytest.mark.asyncio
    async def test_session_id_generated_if_not_given(self):
        llm = make_llm(lambda m: Message(role="assistant", content="ok"))
        agent = make_agent(llm=llm)
        assert agent.session_id
        assert len(agent.session_id) == 8
