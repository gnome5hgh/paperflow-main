# tests/test_audit_i2.py
"""
I2 审计盲点修复测试（spec v2）。

覆盖四个场景（early return 走 after 链，仅审计）：
- I2-1: JSON 解析失败 + AuditMiddleware → "Tool argument parse error" ToolResult，
  审计文件有记录（tool_name 正确、result_status="error"）
- I2-2: 未知工具 + AuditMiddleware → "Unknown tool" ToolResult，
  审计文件有记录（tool_name=幻觉名、risk_level="unknown"）
- I2-3: 未知工具 + 完整中间件链（Audit+Workspace+SecurityScan+PolicyEngine）
  → 不崩溃（各中间件 before 的 None 防御生效）
- I2-4: 非 dict JSON（["hello"]）+ AuditMiddleware → 不崩溃，审计 params == {}
"""

import json
import pytest
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.llm import Message
from paperflow.core.security import (
    AuditMiddleware, WorkspacePolicyMiddleware,
    SecurityScanMiddleware, PolicyEngineMiddleware,
)
from paperflow.core.tool import Tool, ToolResult
from tests.conftest import MockEchoTool


class KwargsTool(Tool):
    """接受任意参数的普通工具，验证非 dict 参数归一化场景。"""

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
        name="test", system_prompt="p", tools=tools or [MockEchoTool()],
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


def read_audit_entries(audit_dir):
    files = list(audit_dir.glob("audit_*.jsonl"))
    assert len(files) == 1
    return [json.loads(line) for line in files[0].read_text().strip().splitlines()]


class TestJsonParseErrorGoesThroughAfterChain:
    """I2-1: JSON 解析失败不再绕过中间件管道——走 after 链审计。"""

    @pytest.mark.asyncio
    async def test_returns_parse_error_and_audits(self, tmp_path):
        agent = make_agent(
            middleware=[AuditMiddleware(audit_dir=str(tmp_path))],
        )
        result = await agent._exec_tool(tool_call("echo", "{not json"))

        assert isinstance(result, ToolResult)
        assert result.text.startswith("Tool argument parse error")

        # 早退路径只走 after：after 补写 tool_started + tool_ended（每调用 2 事件）
        started, ended = read_audit_entries(tmp_path)
        assert started["event_type"] == "tool_started"
        assert started["tool_name"] == "echo"          # 真实工具名，非空
        assert ended["event_type"] == "tool_ended"
        assert ended["tool_name"] == "echo"
        assert ended["result_status"] == "error"


class TestUnknownToolGoesThroughAfterChain:
    """I2-2: 未知工具不再绕过中间件管道——走 after 链审计。"""

    @pytest.mark.asyncio
    async def test_returns_unknown_tool_and_audits(self, tmp_path):
        agent = make_agent(
            middleware=[AuditMiddleware(audit_dir=str(tmp_path))],
        )
        result = await agent._exec_tool(tool_call("nonexistent_tool", "{}"))

        assert isinstance(result, ToolResult)
        assert result.text.startswith("Unknown tool: nonexistent_tool")

        started, ended = read_audit_entries(tmp_path)
        assert started["event_type"] == "tool_started"
        assert ended["event_type"] == "tool_ended"
        assert ended["tool_name"] == "nonexistent_tool"   # 幻觉名原样记录
        assert ended["risk_level"] == "unknown"           # tool=None 时容忍


class TestUnknownToolWithFullChain:
    """I2-3: 未知工具 + 完整中间件链 → 不崩溃（None 防御生效）。"""

    @pytest.mark.asyncio
    async def test_full_chain_does_not_crash(self, tmp_path):
        middleware = [
            AuditMiddleware(audit_dir=str(tmp_path / "audit")),
            WorkspacePolicyMiddleware(workspace=str(tmp_path / "ws")),
            SecurityScanMiddleware(),
            PolicyEngineMiddleware(max_risk="high"),
        ]
        agent = make_agent(middleware=middleware)

        result = await agent._exec_tool(tool_call("nonexistent_tool", "{}"))

        assert isinstance(result, ToolResult)
        assert result.text.startswith("Unknown tool: nonexistent_tool")

        ended = read_audit_entries(tmp_path / "audit")[-1]
        assert ended["event_type"] == "tool_ended"
        assert ended["tool_name"] == "nonexistent_tool"
        assert ended["risk_level"] == "unknown"


class TestNonDictArgsAudited:
    """I2-4: 非 dict JSON（["hello"]）→ 不崩溃，审计 params == {}。"""

    @pytest.mark.asyncio
    async def test_non_dict_args_no_crash_and_empty_params(self, tmp_path):
        agent = make_agent(
            middleware=[AuditMiddleware(audit_dir=str(tmp_path))],
            tools=[KwargsTool()],
        )
        result = await agent._exec_tool(tool_call("kwargstool", '["hello"]'))

        assert isinstance(result, ToolResult)   # 不崩溃

        started, ended = read_audit_entries(tmp_path)
        assert started["event_type"] == "tool_started"
        assert started["params"] == {}            # 非 dict 参数归一化后为空
        assert ended["event_type"] == "tool_ended"
        assert ended["params"] == {}
