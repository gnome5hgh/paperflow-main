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
