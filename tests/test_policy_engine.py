# tests/test_policy_engine.py
import pytest
from paperflow.core.security import ToolContext, PolicyDenied, ConfirmRequired
from paperflow.core.security.policy_engine import PolicyEngineMiddleware
from paperflow.core.tool import Tool, ToolResult


class LowTool(Tool):
    name = "low_tool"
    description = "low risk"
    parameters = {"type": "object", "properties": {}}
    risk_level = "low"

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


class MediumTool(Tool):
    name = "medium_tool"
    description = "medium risk"
    parameters = {"type": "object", "properties": {}}
    risk_level = "medium"

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


class HighTool(Tool):
    name = "high_tool"
    description = "high risk"
    parameters = {"type": "object", "properties": {}}
    risk_level = "high"

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


class ConfirmTool(Tool):
    name = "confirm_tool"
    description = "needs confirm"
    parameters = {"type": "object", "properties": {}}
    risk_level = "medium"
    requires_confirm = True

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


class BlockedTool(Tool):
    name = "blocked_tool"
    description = "blocked by default"
    parameters = {"type": "object", "properties": {}}
    blocked_by_default = True

    def execute(self, **kwargs) -> ToolResult:
        return ToolResult(text="ok")


def make_ctx(tool):
    return ToolContext(
        trace_id="t", session_id="s", agent_type="test",
        tool=tool, tool_name=tool.name, args={},
    )


class TestPolicyEngine:
    @pytest.mark.asyncio
    async def test_allows_low_by_default(self):
        mw = PolicyEngineMiddleware()
        await mw.before(make_ctx(LowTool()))  # 不应抛

    @pytest.mark.asyncio
    async def test_allows_medium_by_default(self):
        mw = PolicyEngineMiddleware()
        await mw.before(make_ctx(MediumTool()))  # 不应抛

    @pytest.mark.asyncio
    async def test_blocks_high_by_default(self):
        mw = PolicyEngineMiddleware()
        with pytest.raises(PolicyDenied) as exc:
            await mw.before(make_ctx(HighTool()))
        assert "风险等级" in exc.value.reason

    @pytest.mark.asyncio
    async def test_unknown_risk_treated_as_critical(self):
        tool = LowTool()
        tool.risk_level = "meduim"  # typo
        mw = PolicyEngineMiddleware()
        with pytest.raises(PolicyDenied):
            await mw.before(make_ctx(tool))

    @pytest.mark.asyncio
    async def test_blocks_blocked_by_default_even_low(self):
        mw = PolicyEngineMiddleware()
        with pytest.raises(PolicyDenied):
            await mw.before(make_ctx(BlockedTool()))

    @pytest.mark.asyncio
    async def test_invalid_max_risk_raises(self):
        with pytest.raises(ValueError):
            PolicyEngineMiddleware(max_risk="extreme")

    @pytest.mark.asyncio
    async def test_confirm_required_first_time(self):
        mw = PolicyEngineMiddleware()
        with pytest.raises(ConfirmRequired) as exc:
            await mw.before(make_ctx(ConfirmTool()))
        assert exc.value.tool_name == "confirm_tool"

    @pytest.mark.asyncio
    async def test_confirm_only_asked_once(self):
        mw = PolicyEngineMiddleware()
        ctx = make_ctx(ConfirmTool())
        with pytest.raises(ConfirmRequired) as exc:
            await mw.before(ctx)
        exc.value.confirm()  # 用户确认
        await mw.before(ctx)  # 不应再抛（_confirmed 生效）

    @pytest.mark.asyncio
    async def test_confirm_scoped_per_path(self):
        # Important 2：write_file 覆盖若只按工具名确认一次，后续任意 note/memory 路径
        # 会被静默写（含误覆盖用户手写笔记）。确认键 = (工具, 路径)——
        # 同工具不同路径仍需单独确认；同路径重试仍只确认一次。
        mw = PolicyEngineMiddleware()
        tool = ConfirmTool()
        ctx_a = make_ctx(tool)
        ctx_a.args = {"path": "/vault/note/a.md"}
        ctx_b = make_ctx(tool)
        ctx_b.args = {"path": "/vault/note/b.md"}
        with pytest.raises(ConfirmRequired) as exc:
            await mw.before(ctx_a)
        exc.value.confirm()                   # 用户确认 a 路径
        await mw.before(ctx_a)                # 同路径重试不再询问
        with pytest.raises(ConfirmRequired):  # 不同路径（b）仍需单独确认
            await mw.before(ctx_b)
