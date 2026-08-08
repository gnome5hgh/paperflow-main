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
        assert entry["event_type"] == "tool_invoked"
        assert entry["span_id"].startswith("span_")

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


class TestAuditTreeFields:
    @pytest.mark.asyncio
    async def test_before_sets_span_and_after_writes_it(self, tmp_path):
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx()
        await mw.before(ctx)
        assert ctx.span_id and ctx.span_id.startswith("span_")
        assert ctx.parent_id is None          # 根调用无父
        assert ctx.depth == 0
        await mw.after(ctx)
        entry = json.loads(next(tmp_path.glob("audit_*.jsonl")).read_text())
        assert entry["event_type"] == "tool_invoked"
        assert entry["span_id"] == ctx.span_id
        assert entry["parent_id"] is None
        assert entry["depth"] == 0

    @pytest.mark.asyncio
    async def test_after_without_before_is_defensive(self, tmp_path):
        # I2 早退路径只走 after：应补 span_id、不炸
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx()
        await mw.after(ctx)
        entry = json.loads(next(tmp_path.glob("audit_*.jsonl")).read_text())
        assert entry["span_id"].startswith("span_")
        assert entry["event_type"] == "tool_invoked"

    @pytest.mark.asyncio
    async def test_error_and_result_and_policy_rules(self, tmp_path):
        from paperflow.core.security import PolicyDenied
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx(error=PolicyDenied("风险等级 high 超过阈值 medium"),
                       policy_context={"max_risk": "medium", "tool_risk": "high"},
                       policy_fired="risk_threshold",
                       result=None)
        await mw.before(ctx); await mw.after(ctx)
        entry = json.loads(next(tmp_path.glob("audit_*.jsonl")).read_text())
        assert entry["error"].startswith("PolicyDenied")
        assert entry["policy_rules"]["fired"] == "risk_threshold"
        assert entry["policy_rules"]["policy_context"]["max_risk"] == "medium"


class TestApprovalEvents:
    @pytest.mark.asyncio
    async def test_requested_then_decided_with_causation(self, tmp_path):
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx(args={"path": "/tmp/x.md"})
        await mw.before(ctx)                       # 工具 span 压栈
        await mw.on_approval(ctx, "requested")
        await mw.on_approval(ctx, "decided", outcome="user_denied")
        await mw.after(ctx)
        lines = [json.loads(l) for l in next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()]
        assert len(lines) == 3
        req, dec, inv = lines
        assert req["event_type"] == "approval_requested"
        assert dec["event_type"] == "approval_decided"
        assert dec["causation_id"] == req["span_id"]     # decided 因果链到 requested
        assert dec["outcome"] == "user_denied"
        assert inv["approval_outcome"] == "user_denied"  # ctx 上由 Agent 填，此处为 None 则省略
        assert inv["parent_id"] == ctx.parent_id         # tool_invoked 父 = 外层 span


class TestLlmCall:
    @pytest.mark.asyncio
    async def test_record_llm_call_writes_event(self, tmp_path):
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        mw.record_llm_call(
            trace_id="trace_x", session_id="s", agent_type="test", turn=1,
            model="deepseek-chat", prompt_tokens=10, completion_tokens=5,
            total_tokens=15, latency_ms=1234, finish_reason="tool_calls")
        entry = json.loads(next(tmp_path.glob("audit_*.jsonl")).read_text())
        assert entry["event_type"] == "llm_call"
        assert entry["model"] == "deepseek-chat"
        assert entry["total_tokens"] == 15
