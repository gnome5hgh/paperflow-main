# tests/test_audit.py
import json
import pytest
from pathlib import Path
from paperflow.core.security.middleware.audit import AuditMiddleware, _sanitize, _derive_decision, _result_status
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


async def test_audit_write_sanitizes_surrogates(tmp_path):
    """审计落盘清洗代理码点：带 surrogate 的参数不炸 JSONL 写入（原会打爆）。"""
    mid = AuditMiddleware(audit_dir=str(tmp_path))
    ctx = make_ctx(args={"question": "a\udce4b"})
    await mid.before(ctx)
    files = list(tmp_path.glob("audit_*.jsonl"))
    assert files, "审计文件应已写出（清洗后不抛写盘失败）"
    line = files[0].read_text(encoding="utf-8")
    assert "a�b" in line

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
        assert len(lines) == 2
        started, ended = (json.loads(l) for l in lines)
        # 一次调用 = 两个事件：before 写 start，after 写 end，同 span
        assert started["event_type"] == "tool_started"
        assert started["span_id"] == ended["span_id"]
        assert ended["event_type"] == "tool_ended"
        assert ended["trace_id"] == "trace_x"
        assert ended["tool_name"] == "audit_tool"
        assert ended["risk_level"] == "medium"
        assert ended["policy_decision"] == "auto_allowed"
        assert ended["result_status"] == "success"
        assert ended["started_at"] == "2026-07-31T10:00:00"
        assert ended["ended_at"]
        assert ended["duration_ms"] >= 0
        assert ended["span_id"].startswith("span_")

    @pytest.mark.asyncio
    async def test_writes_blocked_entry(self, tmp_path):
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx(error=PolicyDenied("no"))
        await mw.before(ctx)
        await mw.after(ctx)

        files = list(tmp_path.glob("audit_*.jsonl"))
        lines = files[0].read_text().strip().splitlines()
        assert len(lines) == 2
        started, ended = (json.loads(l) for l in lines)
        assert started["event_type"] == "tool_started"
        assert ended["event_type"] == "tool_ended"
        assert ended["result_status"] == "policy_blocked"
        assert ended["policy_decision"] == "policy_denied"


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
        lines = [json.loads(l) for l in next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()]
        assert len(lines) == 2
        started, ended = lines
        # before() 写 tool_started：span 建点 + 入参快照（started_at = 调用时间戳）
        assert started["event_type"] == "tool_started"
        assert started["span_id"] == ctx.span_id
        assert started["parent_id"] is None
        assert started["depth"] == 0
        assert started["started_at"] == "2026-07-31T10:00:00"
        assert started["params"] == {}
        # after() 写 tool_ended：同 span 收口 + 耗时/结果
        assert ended["event_type"] == "tool_ended"
        assert ended["span_id"] == ctx.span_id
        assert ended["parent_id"] is None
        assert ended["depth"] == 0
        assert ended["started_at"] == "2026-07-31T10:00:00"
        assert ended["ended_at"]
        assert ended["duration_ms"] >= 0
        assert ended["result_status"] == "success"

    @pytest.mark.asyncio
    async def test_after_without_before_is_defensive(self, tmp_path):
        # I2 早退路径只走 after：应补 span_id + 补写 tool_started，不炸、不弹栈
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx()
        await mw.after(ctx)
        lines = [json.loads(l) for l in next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()]
        assert len(lines) == 2
        started, ended = lines
        assert started["event_type"] == "tool_started"
        assert started["span_id"].startswith("span_")
        assert started["span_id"] == ended["span_id"]   # 补写的 start 与 end 同 span
        assert ended["event_type"] == "tool_ended"

    @pytest.mark.asyncio
    async def test_defensive_after_nests_under_outer_span(self, tmp_path):
        """防御分支父链：嵌套工具（子 agent 幻觉未知工具/坏 JSON）命中 only-after 路径时，
        补写的 span 应挂到外层 span 下——修复前 ctx.parent_id/depth 用默认值，被误写成根节点。"""
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        outer = make_ctx()
        await mw.before(outer)                       # 外层 span 压栈（真实 token）
        phantom = make_ctx(tool=None, tool_name="ghost_tool", args={"x": 1})
        await mw.after(phantom)                      # 防御分支：未写 before，只走 after
        await mw.after(outer)                        # 收口外层 span（弹栈）
        events = [json.loads(l) for l in next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()]
        p_started = [e for e in events if e["event_type"] == "tool_started" and e["tool_name"] == "ghost_tool"][0]
        p_ended = [e for e in events if e["event_type"] == "tool_ended" and e["tool_name"] == "ghost_tool"][0]
        # 防御补写的 start/end 同 span，且父链指向外层 span（不是根节点）
        assert p_started["span_id"] == p_ended["span_id"]
        assert p_started["parent_id"] == outer.span_id
        assert p_started["depth"] == 1
        assert p_ended["parent_id"] == outer.span_id
        assert p_ended["depth"] == 1

    @pytest.mark.asyncio
    async def test_error_and_result_and_policy_rules(self, tmp_path):
        from paperflow.core.security import PolicyDenied
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx(error=PolicyDenied("风险等级 high 超过阈值 medium"),
                       policy_context={"max_risk": "medium", "tool_risk": "high"},
                       policy_fired="risk_threshold",
                       result=None)
        await mw.before(ctx); await mw.after(ctx)
        # 决策/错误/策略依据只在 tool_ended（第二个事件）上
        ended = json.loads(next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()[1])
        assert ended["event_type"] == "tool_ended"
        assert ended["error"].startswith("PolicyDenied")
        assert ended["policy_rules"]["fired"] == "risk_threshold"
        assert ended["policy_rules"]["policy_context"]["max_risk"] == "medium"


class TestApprovalEvents:
    @pytest.mark.asyncio
    async def test_requested_then_decided_with_causation(self, tmp_path):
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        ctx = make_ctx(args={"path": "/tmp/x.md"})
        await mw.before(ctx)                       # 工具 span 压栈（先写 tool_started）
        await mw.on_approval(ctx, "requested")
        await mw.on_approval(ctx, "decided", approval_outcome="user_denied")
        await mw.after(ctx)
        lines = [json.loads(l) for l in next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()]
        assert len(lines) == 4
        started, req, dec, ended = lines
        assert started["event_type"] == "tool_started"
        assert req["event_type"] == "approval_requested"
        assert dec["event_type"] == "approval_decided"
        assert dec["causation_id"] == req["span_id"]     # decided 因果链到 requested
        assert dec["approval_outcome"] == "user_denied"
        assert ended["event_type"] == "tool_ended"
        assert ended["approval_outcome"] == "user_denied"
        assert ended["parent_id"] == ctx.parent_id       # tool_ended 父 = 外层 span


class TestLlmCall:
    @pytest.mark.asyncio
    async def test_record_llm_call_writes_event(self, tmp_path):
        mw = AuditMiddleware(audit_dir=str(tmp_path))
        mw.record_llm_call(
            trace_id="trace_x", session_id="s", agent_type="test", turn=1,
            model="deepseek-chat", prompt_tokens=10, completion_tokens=5,
            total_tokens=15, started_at="2026-07-31T10:00:00", duration_ms=1234,
            finish_reason="tool_calls")
        entry = json.loads(next(tmp_path.glob("audit_*.jsonl")).read_text())
        assert entry["event_type"] == "llm_call"
        assert entry["model"] == "deepseek-chat"
        assert entry["total_tokens"] == 15
        assert entry["started_at"] == "2026-07-31T10:00:00"
        assert entry["duration_ms"] == 1234
        assert entry["ended_at"] == "2026-07-31T10:00:01.234000"
