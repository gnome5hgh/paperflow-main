# tests/test_audit_robustness.py
"""
审计管道的崩溃路径回归测试（final review findings 修复）。

覆盖三个场景：
- C1: 工具执行抛普通异常（RuntimeError）→ 不抛 AttributeError，
  _exec_tool 返回 "Tool error" ToolResult，审计 entry result_status="error"
- C2: LLM 返回非 dict JSON 参数（如 ["hello"]）→ _sanitize 不崩溃，
  审计照常写入（params 脱敏为 {}）
- I1: after 钩子抛异常 → 不中止工具结果返回，其余 after 钩子仍执行
"""

import json
import pytest
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.llm import Message
from paperflow.core.security import AuditMiddleware, SecurityMiddleware
from paperflow.core.tool import Tool, ToolResult


class BoomTool(Tool):
    """execute 总是抛普通异常（RuntimeError），非 SecurityError。"""

    name = "boom"
    description = "always raises"
    parameters = {"type": "object", "properties": {}}
    risk_level = "high"

    def execute(self, **kwargs) -> ToolResult:
        raise RuntimeError("boom")


class KwargsTool(Tool):
    """接受任意参数的普通工具，用于验证非 dict 参数场景。"""

    name = "kwargstool"
    description = "accepts anything"
    parameters = {"type": "object", "properties": {}}
    risk_level = "low"

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


def make_llm(responder):
    mock = MagicMock()

    async def chat(messages, tools=None, tool_choice="auto"):
        return responder(messages)

    mock.chat = chat
    return mock


def make_agent(middleware=None, tools=None):
    registry = MagicMock(spec=AgentRegistry)
    registry.get_config.return_value = AgentConfig(
        name="test", system_prompt="p", tools=tools or [BoomTool()],
    )
    return Agent(
        llm=make_llm(lambda m: Message(role="assistant", content="done")),
        agent_registry=registry,
        agent_type="test",
        security_middleware=middleware or [],
    )


def tool_call(name, args_json):
    return {
        "id": "call_1", "type": "function",
        "function": {"name": name, "arguments": args_json},
    }


def read_audit_entries(tmp_path):
    files = list(tmp_path.glob("audit_*.jsonl"))
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text().strip().splitlines()]


class TestToolExceptionDoesNotCrashAudit:
    """C1: 工具抛普通异常 + AuditMiddleware → 错误 ToolResult + result_status=error"""

    @pytest.mark.asyncio
    async def test_exec_tool_returns_tool_error_without_attributeerror(self, tmp_path):
        agent = make_agent(
            middleware=[AuditMiddleware(audit_dir=str(tmp_path))],
        )
        result = await agent._exec_tool(tool_call("boom", "{}"))
        assert result.text == "Tool error: boom"
        assert isinstance(result, ToolResult)

    @pytest.mark.asyncio
    async def test_audit_entry_records_result_status_error(self, tmp_path):
        agent = make_agent(
            middleware=[AuditMiddleware(audit_dir=str(tmp_path))],
        )
        await agent._exec_tool(tool_call("boom", "{}"))

        entry = read_audit_entries(tmp_path)[0]
        assert entry["tool_name"] == "boom"
        assert entry["result_status"] == "error"
        assert entry["policy_decision"] == "error"


class TestNonDictArgsDoNotCrashSanitize:
    """C2: LLM 返回非 dict JSON 参数（如 ["hello"]）→ 不 crash"""

    @pytest.mark.asyncio
    async def test_list_args_returns_toolresult(self, tmp_path):
        agent = make_agent(
            middleware=[AuditMiddleware(audit_dir=str(tmp_path))],
            tools=[KwargsTool()],
        )
        result = await agent._exec_tool(tool_call("kwargstool", '["hello"]'))
        assert isinstance(result, ToolResult)
        assert result.text.startswith("Tool error")  # **list 非法，转错误结果

    @pytest.mark.asyncio
    async def test_audit_entry_written_with_sanitized_empty_params(self, tmp_path):
        agent = make_agent(
            middleware=[AuditMiddleware(audit_dir=str(tmp_path))],
            tools=[KwargsTool()],
        )
        await agent._exec_tool(tool_call("kwargstool", '["hello"]'))

        entry = read_audit_entries(tmp_path)[0]
        assert entry["params"] == {}  # 非 dict 参数被 _sanitize 安全降级


class TestAfterHookFailureIsolated:
    """I1: after 钩子抛异常 → 不中止工具结果，其余 after 钩子仍执行"""

    @pytest.mark.asyncio
    async def test_failing_after_does_not_abort_exec_tool(self):
        class FailAfter(SecurityMiddleware):
            async def after(self, ctx):
                raise RuntimeError("after boom")

        agent = make_agent(middleware=[FailAfter()])
        result = await agent._exec_tool(tool_call("boom", "{}"))
        assert result.text == "Tool error: boom"  # 工具结果正常返回

    @pytest.mark.asyncio
    async def test_other_after_hook_still_runs(self):
        class FailAfter(SecurityMiddleware):
            async def after(self, ctx):
                raise RuntimeError("after boom")

        seen = []

        class RecordAfter(SecurityMiddleware):
            async def after(self, ctx):
                seen.append(ctx.tool_name)

        agent = make_agent(middleware=[RecordAfter(), FailAfter()])
        result = await agent._exec_tool(tool_call("boom", "{}"))
        assert result.text == "Tool error: boom"
        assert seen == ["boom"]  # 失败的 after 之后，正常 after 仍执行
