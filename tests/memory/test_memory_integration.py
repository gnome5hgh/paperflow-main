# tests/test_memory_integration.py
# Task 11 集成测试：验证完整组装（store/git/structured/compressor/五中间件/dream）
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
from paperflow.core.memory import (
    MemoryStore, ExperienceMemoryMiddleware, MemoryIndex,
    ContextCompressor, GitStore,
)
from paperflow.core.structured import StructuredOutput
from paperflow.core.tool import Tool, ToolResult


class WriteTool(Tool):
    name = "write_note"
    description = "w"
    parameters = {
        "type": "object",
        "properties": {
            "content": {"type": "string", "format": "content"},
        },
        "required": ["content"],
    }
    risk_level = "medium"
    requires_confirm = True
    side_effects = ["write_file"]

    def execute(self, content="") -> ToolResult:
        return ToolResult(text="wrote", summary={"note_len": len(content)})


def build_full(memory_dir, workspace, audit_dir):
    store = MemoryStore(memory_dir)
    middlewares = [
        AuditMiddleware(audit_dir=audit_dir),
        WorkspacePolicyMiddleware(workspace=workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk="high"),
        ExperienceMemoryMiddleware(store),
    ]
    return store, middlewares


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_tool_call_records_experience(self, tmp_path):
        memory_dir = tmp_path / "memory"
        workspace = str(tmp_path / "ws")
        audit_dir = str(tmp_path / "audit")
        store, middlewares = build_full(memory_dir, workspace, audit_dir)

        registry = MagicMock(spec=AgentRegistry)
        registry.get_config.return_value = AgentConfig(
            name="test", system_prompt="p", tools=[WriteTool()],
        )
        llm = MagicMock()
        async def chat(messages, tools=None, tool_choice="auto", **kw):
            return Message(role="assistant", content="done")
        llm.chat = chat
        llm.context_window = 65536

        async def confirm(cr):
            return True

        agent = Agent(llm=llm, agent_registry=registry, agent_type="test",
                      security_middleware=middlewares, confirm_callback=confirm,
                      memory_index=MemoryIndex(memory_dir))
        result = await agent._exec_tool({
            "id": "c1",
            "function": {"name": "write_note", "arguments": '{"content": "note"}'},
        })
        assert result.text == "wrote"
        entries = store.read_unprocessed_history(since=0)
        assert any(e["type"] == "tool" and e["tool_name"] == "write_note" for e in entries)
        assert entries[-1]["summary"] == {"note_len": 4}

    @pytest.mark.asyncio
    async def test_denied_call_recorded_with_error_type(self, tmp_path):
        memory_dir = tmp_path / "memory"
        store, middlewares = build_full(memory_dir, str(tmp_path / "ws"),
                                        str(tmp_path / "audit"))
        # requires_confirm 但 confirm 拒绝
        registry = MagicMock(spec=AgentRegistry)
        registry.get_config.return_value = AgentConfig(
            name="test", system_prompt="p", tools=[WriteTool()],
        )
        llm = MagicMock()
        async def chat(messages, tools=None, tool_choice="auto", **kw):
            return Message(role="assistant", content="done")
        llm.chat = chat
        llm.context_window = 65536

        async def confirm(cr):
            return False

        agent = Agent(llm=llm, agent_registry=registry, agent_type="test",
                      security_middleware=middlewares, confirm_callback=confirm)
        result = await agent._exec_tool({
            "id": "c1",
            "function": {"name": "write_note", "arguments": '{"content": "note"}'},
        })
        assert result.summary["decision"] == "user_denied"
        entries = store.read_unprocessed_history(since=0)
        assert entries[-1]["type"] == "tool"
        assert entries[-1]["success"] is False
        assert entries[-1]["error_type"] == "user_denied"

    def test_memory_package_exports_all(self):
        from paperflow.core.memory import (
            MemoryStore, ExperienceMemoryMiddleware, MemoryIndex,
            ContextCompressor, GitStore, Dream, DreamEdit, DreamEditBatch,
            ContextConfig, SummarySchema,
        )
        assert MemoryStore and Dream and ContextCompressor
