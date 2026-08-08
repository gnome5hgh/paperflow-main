# tests/test_middleware_protocol.py
import pytest
from paperflow.core.security import (
    SecurityMiddleware, ToolContext, SecurityError,
    PolicyDenied, ConfirmRequired, SecurityBlocked,
)
from paperflow.core.tool import Tool, ToolResult


class DummyTool(Tool):
    name = "dummy"
    description = "test"
    parameters = {"type": "object", "properties": {}}

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


def make_ctx():
    return ToolContext(
        trace_id="trace_abc",
        session_id="sess_1",
        agent_type="test",
        tool=DummyTool(),
        tool_name="dummy",
        args={},
    )


class TestToolContext:
    def test_defaults(self):
        ctx = make_ctx()
        assert ctx.timestamp is None
        assert ctx.started_at is None
        assert ctx.result is None
        assert ctx.error is None
        assert ctx.user_confirmed is False


class TestMiddlewareProtocol:
    def test_default_before_noop(self):
        mw = SecurityMiddleware()
        ctx = make_ctx()
        import asyncio
        asyncio.run(mw.before(ctx))  # 不应抛

    def test_default_after_noop(self):
        mw = SecurityMiddleware()
        ctx = make_ctx()
        import asyncio
        asyncio.run(mw.after(ctx))

    def test_default_on_finish_passthrough(self):
        mw = SecurityMiddleware()
        import asyncio
        result = asyncio.run(mw.on_finish(None, "hello"))
        assert result == "hello"


class TestExceptions:
    def test_policy_denied(self):
        e = PolicyDenied("no")
        assert e.decision == "policy_denied"
        assert e.reason == "no"

    def test_security_blocked_carries_violations(self):
        e = SecurityBlocked("bad", [{"rule_id": "x", "severity": "critical"}])
        assert e.decision == "security_blocked"
        assert e.violations[0]["rule_id"] == "x"

    def test_confirm_required_with_callback(self):
        called = []
        cr = ConfirmRequired(
            tool_name="write_file",
            params={"path": "x"},
            risk_level="medium",
            side_effects=["write_file"],
            on_confirmed=lambda: called.append(True),
        )
        assert cr.decision == "confirm_required"
        cr.confirm()
        assert called == [True]

    def test_confirm_required_without_callback(self):
        cr = ConfirmRequired("t", {}, "low", [])
        cr.confirm()  # 不应抛
