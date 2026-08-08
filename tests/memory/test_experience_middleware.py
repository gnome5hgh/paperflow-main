import pytest
from pathlib import Path
from paperflow.core.memory.experience_memory import (
    MemoryStore, ExperienceMemoryMiddleware, _error_type,
)
from paperflow.core.security import (
    ToolContext, PolicyDenied, SecurityBlocked, ConfirmRequired,
)
from paperflow.core.tool import Tool, ToolResult


class ExpTool(Tool):
    name = "exp_tool"
    description = "test"
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok", summary={"note": "valuable"})


class FailingTool(Tool):
    name = "fail_tool"
    description = "test"
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        raise RuntimeError("boom")


def make_ctx(tool, error=None, result=None, started_at=0.0):
    return ToolContext(
        trace_id="t", session_id="s", agent_type="test",
        tool=tool, tool_name=(tool.name if tool else ""), args={},
        started_at=started_at, result=result, error=error,
    )


class TestErrorType:
    def test_none(self):
        assert _error_type(None) == ""

    def test_policy_denied(self):
        assert _error_type(PolicyDenied("no")) == "policy_denied"

    def test_security_blocked(self):
        assert _error_type(SecurityBlocked("bad", [])) == "security_blocked"

    def test_confirm_required(self):
        assert _error_type(ConfirmRequired("t", {}, "low", [])) == "user_denied"

    def test_generic(self):
        assert _error_type(RuntimeError("x")) == "exec_error"


class TestExperienceMiddleware:
    @pytest.mark.asyncio
    async def test_records_successful_tool_call(self, tmp_path):
        store = MemoryStore(tmp_path)
        mw = ExperienceMemoryMiddleware(store)
        ctx = make_ctx(ExpTool(), result=ToolResult(text="ok", summary={"note": "v"}))
        await mw.after(ctx)
        entries = store.read_unprocessed_history(since=0)
        assert entries[0]["type"] == "tool"
        assert entries[0]["tool_name"] == "exp_tool"
        assert entries[0]["success"] is True
        assert entries[0]["summary"] == {"note": "v"}
        assert entries[0]["error_type"] == ""

    @pytest.mark.asyncio
    async def test_records_denied_call(self, tmp_path):
        store = MemoryStore(tmp_path)
        mw = ExperienceMemoryMiddleware(store)
        ctx = make_ctx(ExpTool(), error=PolicyDenied("no"))
        await mw.after(ctx)
        entries = store.read_unprocessed_history(since=0)
        assert entries[0]["success"] is False
        assert entries[0]["error_type"] == "policy_denied"

    @pytest.mark.asyncio
    async def test_records_tool_error(self, tmp_path):
        store = MemoryStore(tmp_path)
        mw = ExperienceMemoryMiddleware(store)
        ctx = make_ctx(FailingTool(), error=RuntimeError("boom"),
                       result=ToolResult(text="Tool error: boom"))
        await mw.after(ctx)
        entries = store.read_unprocessed_history(since=0)
        assert entries[0]["success"] is False
        assert entries[0]["error_type"] == "exec_error"

    @pytest.mark.asyncio
    async def test_skips_when_tool_none(self, tmp_path):
        store = MemoryStore(tmp_path)
        mw = ExperienceMemoryMiddleware(store)
        ctx = make_ctx(None)   # tool=None：未知工具由 Audit 覆盖
        await mw.after(ctx)
        assert store.read_unprocessed_count() == 0
