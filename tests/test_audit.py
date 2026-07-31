# tests/test_audit.py
import json
import pytest
from pathlib import Path
from paperflow.core.security.audit import AuditMiddleware, _sanitize, _derive_decision, _result_status
from paperflow.core.security import (
    ToolContext, PolicyDenied, SecurityBlocked, ConfirmRequired,
)
from paperflow.core.tool import Tool, ToolResult


class AuditTool(Tool):
    name = "audit_tool"
    description = "test"
    parameters = {"type": "object", "properties": {}}
    risk_level = "medium"

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


def make_ctx(**overrides):
    defaults = dict(
        trace_id="trace_x", session_id="sess_1", agent_type="test",
        tool=AuditTool(), tool_name="audit_tool", args={},
        timestamp="2026-07-31T10:00:00", started_at=0.0,
    )
    defaults.update(overrides)
    return ToolContext(**defaults)


class TestSanitize:
    def test_masks_keys(self):
        out = _sanitize({"api_key": "sk-abc", "content": "long text", "query": "x"})
        assert out["api_key"] == "***"
        assert out["content"] == "HIDDEN"
        assert out["query"] == "x"

    def test_paths_kept(self):
        out = _sanitize({"pdf_path": "/abs/paper/pdf/x.pdf", "query": "y"})
        assert "pdf_path" in out


class TestDecisions:
    def test_auto_allowed(self):
        ctx = make_ctx()
        assert _derive_decision(ctx) == "auto_allowed"

    def test_user_confirmed(self):
        ctx = make_ctx(user_confirmed=True)
        assert _derive_decision(ctx) == "user_confirmed"

    def test_policy_denied(self):
        ctx = make_ctx(error=PolicyDenied("no"))
        assert _derive_decision(ctx) == "policy_denied"
        assert _result_status(ctx) == "policy_blocked"

    def test_security_blocked(self):
        ctx = make_ctx(error=SecurityBlocked("bad", [{"rule_id": "x"}]))
        assert _derive_decision(ctx) == "security_blocked"
        assert _result_status(ctx) == "security_blocked"

    def test_confirm_required_maps_to_user_denied(self):
        ctx = make_ctx(error=ConfirmRequired("t", {}, "low", []))
        assert _derive_decision(ctx) == "user_denied"
        assert _result_status(ctx) == "user_denied"


class TestAuditMiddleware:
    @pytest.mark.asyncio
    async def test_writes_jsonl_entry(self, tmp_path):
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx(args={"query": "circRNA"})
        await mw.before(ctx)
        await mw.after(ctx)

        files = list(tmp_path.glob("audit_*.jsonl"))
        assert len(files) == 1
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["trace_id"] == "trace_x"
        assert entry["tool_name"] == "audit_tool"
        assert entry["risk_level"] == "medium"
        assert entry["policy_decision"] == "auto_allowed"
        assert entry["result_status"] == "success"
        assert entry["timestamp"] == "2026-07-31T10:00:00"

    @pytest.mark.asyncio
    async def test_writes_blocked_entry(self, tmp_path):
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx(error=PolicyDenied("no"))
        await mw.before(ctx)
        await mw.after(ctx)

        files = list(tmp_path.glob("audit_*.jsonl"))
        entry = json.loads(files[0].read_text().strip())
        assert entry["result_status"] == "policy_blocked"
        assert entry["policy_decision"] == "policy_denied"
