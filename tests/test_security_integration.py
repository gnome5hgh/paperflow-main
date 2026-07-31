import json
import pytest
from pathlib import Path
from unittest.mock import MagicMock
from paperflow.core.agent import Agent
from paperflow.core.llm import Message
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.security import (
    AuditMiddleware, WorkspacePolicyMiddleware,
    SecurityScanMiddleware, PolicyEngineMiddleware,
)
from paperflow.core.tool import Tool, ToolResult


class WriteNoteTool(Tool):
    name = "write_note"
    description = "write a note"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path"},
            "content": {"type": "string", "format": "content"},
        },
        "required": ["path", "content"],
    }
    risk_level = "medium"
    requires_confirm = True
    side_effects = ["write_file"]
    allowed_paths = ["paper/note/"]

    def execute(self, path, content) -> ToolResult:
        return ToolResult(text=f"wrote {path}")


def build_full_middleware(workspace, audit_dir):
    return [
        AuditMiddleware(audit_dir=audit_dir),
        WorkspacePolicyMiddleware(workspace=workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk="high"),
    ]


def make_agent(middleware, confirm_cb, tools=None, audit_dir=None):
    tools = tools or [WriteNoteTool()]
    registry = MagicMock(spec=AgentRegistry)
    registry.get_config.return_value = AgentConfig(
        name="test", system_prompt="p", tools=tools,
    )
    llm = MagicMock()
    async def chat(messages, tools=None, tool_choice="auto"):
        return Message(role="assistant", content="done")
    llm.chat = chat
    return Agent(
        llm=llm, agent_registry=registry, agent_type="test",
        security_middleware=middleware, confirm_callback=confirm_cb,
    )


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_legit_write_with_confirm(self, tmp_path):
        workspace = str(tmp_path / "ws")
        audit_dir = str(tmp_path / "audit")
        accepted = []
        async def confirm(cr):
            accepted.append(cr.tool_name)
            return True

        agent = make_agent(
            build_full_middleware(workspace, audit_dir), confirm,
        )
        result = await agent._exec_tool({
            "id": "c1",
            "function": {
                "name": "write_note",
                "arguments": '{"path": "paper/note/a.md", "content": "note body"}',
            },
        })
        assert result.text == "wrote paper/note/a.md"
        assert accepted == ["write_note"]

        # 审计记录了 user_confirmed
        entries = []
        for f in Path(audit_dir).glob("audit_*.jsonl"):
            entries.extend(json.loads(line) for line in f.read_text().strip().splitlines())
        assert entries[0]["policy_decision"] == "user_confirmed"
        assert entries[0]["result_status"] == "success"

    @pytest.mark.asyncio
    async def test_path_escape_blocked_and_audited(self, tmp_path):
        workspace = str(tmp_path / "ws")
        audit_dir = str(tmp_path / "audit")
        async def confirm(cr):
            return True

        agent = make_agent(
            build_full_middleware(workspace, audit_dir), confirm,
        )
        result = await agent._exec_tool({
            "id": "c1",
            "function": {
                "name": "write_note",
                "arguments": '{"path": "../../etc/passwd", "content": "x"}',
            },
        })
        assert result.summary["decision"] == "security_blocked"

        entries = []
        for f in Path(audit_dir).glob("audit_*.jsonl"):
            entries.extend(json.loads(line) for line in f.read_text().strip().splitlines())
        assert entries[0]["result_status"] == "security_blocked"
        assert entries[0]["security_scan"] is not None

    @pytest.mark.asyncio
    async def test_dangerous_content_blocked_before_confirm(self, tmp_path):
        # SecurityScan 在 PolicyEngine 之前 —— 内容有毒时不应问用户确认
        workspace = str(tmp_path / "ws")
        audit_dir = str(tmp_path / "audit")
        asked = []
        async def confirm(cr):
            asked.append(cr.tool_name)
            return True

        agent = make_agent(
            build_full_middleware(workspace, audit_dir), confirm,
        )
        result = await agent._exec_tool({
            "id": "c1",
            "function": {
                "name": "write_note",
                "arguments": '{"path": "paper/note/a.md", "content": "run rm -rf /"}',
            },
        })
        assert result.summary["decision"] == "security_blocked"
        assert asked == []  # 没问确认
