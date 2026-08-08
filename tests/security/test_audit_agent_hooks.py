# tests/security/test_audit_agent_hooks.py
"""Agent 侧审计接线：审批双事件 + llm_call + turn 透传。"""
import json
import pytest
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.llm import Message
from paperflow.core.security import (
    AuditMiddleware, ConfirmRequired, ToolContext, SecurityMiddleware,
)
from paperflow.core.tool import Tool, ToolResult
from tests.conftest import MockEchoTool


class ConfirmTool(Tool):
    name = "confirm_tool"
    description = "needs confirm"
    parameters = {"type": "object", "properties": {}}
    risk_level = "high"
    requires_confirm = True

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


class ConfirmMW(SecurityMiddleware):
    """按工具 requires_confirm 声明触发 ConfirmRequired（模拟 PolicyEngine 的确认分支）。

    测试只挂 AuditMiddleware 时没有任何中间件会抛 ConfirmRequired（仅
    PolicyEngineMiddleware 对 requires_confirm 工具抛），审批分支无从触发——
    故补一个最小中间件专供 Agent 接线测试驱动确认流程。
    """
    async def before(self, ctx):
        if ctx.tool is not None and ctx.tool.requires_confirm:
            raise ConfirmRequired(
                ctx.tool_name, ctx.args, ctx.tool.risk_level, ctx.tool.side_effects)


def tool_call(name, args_json='{}'):
    return {"id": "call_1", "type": "function",
            "function": {"name": name, "arguments": args_json}}


def make_agent(middleware, tools, confirm_callback):
    registry = MagicMock(spec=AgentRegistry)
    registry.get_config.return_value = AgentConfig(
        name="test", system_prompt="p", tools=tools)
    return Agent(llm=make_llm(), agent_registry=registry, agent_type="test",
                 security_middleware=middleware, confirm_callback=confirm_callback)


def make_llm():
    mock = MagicMock()
    async def chat(messages, tools=None, tool_choice="auto", telemetry_callback=None):
        return Message(role="assistant", content="done")
    mock.chat = chat
    return mock


def read_entries(tmp_path):
    return [json.loads(l) for l in next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()]


class TestApprovalFlowFromAgent:
    @pytest.mark.asyncio
    async def test_denied_emits_requested_and_decided_and_tool(self, tmp_path):
        async def deny(cr):            # 真实用户回调（非默认）
            return False
        agent = make_agent(
            [AuditMiddleware(audit_dir=str(tmp_path)), ConfirmMW()],
            [ConfirmTool()], deny)
        result = await agent._exec_tool(tool_call("confirm_tool"))
        assert result.summary["decision"] == "user_denied"
        events = read_entries(tmp_path)
        types = [e["event_type"] for e in events]
        # 审批流每工具 4 事件：start → request → decide → end
        assert types == ["tool_started", "approval_requested", "approval_decided", "tool_ended"]
        dec = [e for e in events if e["event_type"] == "approval_decided"][0]
        assert dec["approval_outcome"] == "user_denied"
        inv = [e for e in events if e["event_type"] == "tool_ended"][0]
        assert inv["approval_outcome"] == "user_denied"
        assert inv["causation_id"] == dec["span_id"]

    @pytest.mark.asyncio
    async def test_default_confirm_is_auto_denied(self, tmp_path):
        agent = make_agent([AuditMiddleware(audit_dir=str(tmp_path)), ConfirmMW()],
                           [ConfirmTool()], confirm_callback=None)   # 默认 fail-safe
        await agent._exec_tool(tool_call("confirm_tool"))
        dec = [e for e in read_entries(tmp_path) if e["event_type"] == "approval_decided"][0]
        assert dec["approval_outcome"] == "auto_denied"

    @pytest.mark.asyncio
    async def test_confirmed_path_sets_approval_outcome(self, tmp_path):
        async def yes(cr):
            return True
        agent = make_agent([AuditMiddleware(audit_dir=str(tmp_path)), ConfirmMW()],
                           [ConfirmTool()], yes)
        await agent._exec_tool(tool_call("confirm_tool"))
        inv = [e for e in read_entries(tmp_path) if e["event_type"] == "tool_ended"][0]
        assert inv["approval_outcome"] == "user_confirmed"


class TestTurnPropagation:
    @pytest.mark.asyncio
    async def test_turn_reaches_audit_entry(self, tmp_path):
        async def deny(cr):
            return False
        agent = make_agent([AuditMiddleware(audit_dir=str(tmp_path)), ConfirmMW()],
                           [ConfirmTool()], deny)
        await agent._exec_tool(tool_call("confirm_tool"), turn=3)
        events = read_entries(tmp_path)
        assert all(e["turn"] == 3 for e in events)


class TestLlmCallFromAgent:
    @pytest.mark.asyncio
    async def test_agent_passes_telemetry_callback(self):
        captured = {}

        class FakeLLM:
            async def chat(self, messages, tools=None, tool_choice="auto", telemetry_callback=None):
                captured["cb"] = telemetry_callback
                return Message(role="assistant", content="done")

        registry = MagicMock(spec=AgentRegistry)
        registry.get_config.return_value = AgentConfig(
            name="test", system_prompt="p", tools=[MockEchoTool()])
        agent = Agent(llm=FakeLLM(), agent_registry=registry, agent_type="test",
                      security_middleware=[])
        # 触发一次 run（无工具调用 → 最终回答）
        out = await agent.run("hi")
        assert out == "done"
        assert captured["cb"] is not None        # Agent 向 LLM 传了回调


class SpyMW(SecurityMiddleware):
    """spy 中间件:记录 record_llm_call 收到的字段,验证 _emit_llm_call 的 fan-out。"""

    def __init__(self):
        self.calls = []

    def record_llm_call(self, **fields):
        self.calls.append(fields)


class TestEmitLlmCall:
    @pytest.mark.asyncio
    async def test_emit_llm_call_fans_out_to_middleware(self):
        agent = make_agent([SpyMW()], [MockEchoTool()], confirm_callback=None)
        agent._trace_id = "trace_abc"
        agent._emit_llm_call(3, {"model": "m", "total_tokens": 5})
        spy = agent.security_middleware[0]
        assert len(spy.calls) == 1
        fields = spy.calls[0]
        assert fields["trace_id"] == "trace_abc"
        assert fields["session_id"] == agent.session_id
        assert fields["agent_type"] == "test"
        assert fields["turn"] == 3
        assert fields["model"] == "m"
        assert fields["total_tokens"] == 5

    @pytest.mark.asyncio
    async def test_make_llm_telemetry_delegates_to_emit(self):
        agent = make_agent([SpyMW()], [MockEchoTool()], confirm_callback=None)
        agent._trace_id = "trace_def"
        agent._make_llm_telemetry(2)({"model": "m"})
        spy = agent.security_middleware[0]
        assert len(spy.calls) == 1
        assert spy.calls[0]["trace_id"] == "trace_def"
        assert spy.calls[0]["turn"] == 2

    @pytest.mark.asyncio
    async def test_run_tracks_current_turn(self):
        """run() 每轮更新 _current_turn:spawn 摘要提取据此归属父的当前轮次。"""
        registry = MagicMock(spec=AgentRegistry)
        registry.get_config.return_value = AgentConfig(
            name="test", system_prompt="p", tools=[MockEchoTool()])
        llm = MagicMock()
        responses = iter([
            Message(role="assistant", content=None, tool_calls=[
                {"id": "c1", "type": "function",
                 "function": {"name": "echo", "arguments": '{"message": "hi"}'}}]),
            Message(role="assistant", content="done"),
        ])

        async def chat(messages, tools=None, tool_choice="auto", telemetry_callback=None):
            return next(responses)
        llm.chat = chat

        agent = Agent(llm=llm, agent_registry=registry, agent_type="test",
                      security_middleware=[])
        assert agent._current_turn == 0
        out = await agent.run("hi")
        assert out == "done"
        assert agent._current_turn == 1        # 第二轮是最终回答所在轮
