"""Supervisor 4 个调度工具测试（mock child，无真实 LLM）。"""
import asyncio
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperflow.core.agent import Agent, MaxTurnsExceeded, StreamEvent
from paperflow.core.llm import Message
from paperflow.core.security import PolicyEngineMiddleware
from paperflow.core.tool import Tool, ToolResult
from tests.conftest import _tc
from tests.test_agent import make_mock_llm, make_mock_registry

from agents.supervisor.tools import (
    SpawnSubAgentTool, ParallelSpawnTool, AggregateResultsTool, AskUserTool,
    _UserWaitClock, _wrap_confirm_callback, _run_child_with_budget,
)


def _supervisor(tools, **kwargs):
    """构造带指定工具的 Agent（agent_type="supervisor"），可注入 confirm/ask 回调。

    默认 llm 是空响应 mock——这些测试直接调 tool.execute()，不走 Agent.run()。"""
    llm = kwargs.pop("llm", None) or make_mock_llm([])
    return Agent(llm=llm, agent_registry=make_mock_registry(tools),
                 agent_type="supervisor", **kwargs)


class TestSpawnSubAgentTool:
    def test_needs_parent_injected(self):
        tool = SpawnSubAgentTool()
        agent = _supervisor([tool])
        assert tool._parent is agent

    def test_confirm_callback_passed_to_child(self):
        """D6 关键断言：child 构造必须继承父 confirm_callback（generate-note 写盘靠它）。"""
        with patch("agents.supervisor.tools.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(return_value="done")
            cb = lambda cr: True
            agent = _supervisor([SpawnSubAgentTool()], confirm_callback=cb)
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="search-paper", task="搜索 x")
        kwargs = MockAgent.call_args.kwargs
        assert kwargs["confirm_callback"] is cb
        assert kwargs["agent_type"] == "search-paper"
        assert kwargs["security_middleware"] == agent.security_middleware
        assert kwargs["session_id"] == agent.session_id
        parsed = json.loads(result.text)
        assert parsed["status"] == "success"
        assert parsed["summary"] == "done"

    def test_timeout_maps_to_timeout_status(self):
        old = SpawnSubAgentTool.timeout
        SpawnSubAgentTool.timeout = 0.05       # 覆盖 120s 默认（类属性，测试后还原防泄漏）
        try:
            with patch("agents.supervisor.tools.Agent") as MockAgent:
                async def hang(*a, **k):
                    await asyncio.sleep(5)
                MockAgent.return_value.run = hang
                agent = _supervisor([SpawnSubAgentTool()])
                result = agent.tools["spawn_sub_agent"].execute(
                    agent_type="search-paper", task="t")
            # M3：断言用执行期生效的 self.timeout（0.05）插值——写死 "120s" 会与
            # 类属性覆盖后的真实值漂移，无法防插值回归
            expected = f"SubAgent 在 {SpawnSubAgentTool.timeout}s 内未完成"
        finally:
            SpawnSubAgentTool.timeout = old
        parsed = json.loads(result.text)
        assert parsed["status"] == "timeout"
        assert parsed["error_detail"] == expected

    def test_max_turns_maps_to_failed(self):
        with patch("agents.supervisor.tools.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(side_effect=MaxTurnsExceeded("boom"))
            agent = _supervisor([SpawnSubAgentTool()])
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="search-paper", task="t")
        parsed = json.loads(result.text)
        assert parsed["status"] == "failed"
        assert "boom" in parsed["error_detail"]

    def test_non_supervisor_parent_denied(self):
        """allowed_spawns 运行时校验：非 supervisor parent 拒绝越界 spawn（ADR 0003）。"""
        tool = SpawnSubAgentTool()
        registry = make_mock_registry([tool])     # allowed_spawns 缺省 []
        agent = Agent(llm=make_mock_llm([], ), agent_registry=registry,
                      agent_type="generate-note")
        result = tool.execute(agent_type="review-note", task="审稿")
        parsed = json.loads(result.text)
        assert parsed["status"] == "denied"
        assert "不能 spawn" in parsed["summary"]

    def test_resolve_timeout_map_and_fallback(self):
        """D2：config 按 agent 命中优先；未命中 fallback 到类默认（M3 seam 保留）。"""
        tool = SpawnSubAgentTool(agent_timeouts={"generate-note": 300})
        assert tool._resolve_timeout("generate-note") == 300
        assert tool._resolve_timeout("search-paper") == 120      # 类默认

    def test_timeout_uses_agent_timeouts_map(self):
        """config 命中时 error_detail 插值用 map 值而非类默认（防漂移）。"""
        with patch("agents.supervisor.tools.Agent") as MockAgent:
            async def hang(*a, **k):
                await asyncio.sleep(5)
            MockAgent.return_value.run = hang
            tool = SpawnSubAgentTool(agent_timeouts={"search-paper": 0.05})
            agent = _supervisor([tool])
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="search-paper", task="t")
        parsed = json.loads(result.text)
        assert parsed["status"] == "timeout"
        assert parsed["error_detail"] == "SubAgent 在 0.05s 内未完成"


class TestParallelSpawnTool:
    def test_per_child_isolation(self):
        """一个 child 失败只映射自身，不拖垮其他（spec 🟠3）。"""
        with patch("agents.supervisor.tools.Agent") as MockAgent:
            async def run_mixed(task):
                if "fail" in task:
                    raise MaxTurnsExceeded("boom")
                return f"ok:{task}"
            MockAgent.return_value.run = run_mixed
            agent = _supervisor([ParallelSpawnTool()])
            result = agent.tools["parallel_spawn"].execute(spawns=[
                {"agent_type": "a", "task": "ok1"},
                {"agent_type": "b", "task": "fail"},
            ])
        parsed = json.loads(result.text)
        assert len(parsed) == 2
        by_status = {p["status"] for p in parsed}
        assert by_status == {"success", "failed"}

    def test_parallel_success_all(self):
        with patch("agents.supervisor.tools.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(side_effect=lambda task: f"r:{task}")
            agent = _supervisor([ParallelSpawnTool()])
            result = agent.tools["parallel_spawn"].execute(spawns=[
                {"agent_type": "a", "task": "t1"},
                {"agent_type": "a", "task": "t2"},
            ])
        parsed = json.loads(result.text)
        assert [p["status"] for p in parsed] == ["success", "success"]

    def test_denied_when_spawn_not_allowed(self):
        """R1 修复：ParallelSpawn 越界 spawn 也返回 per-child denied（与 Spawn 校验对齐）。

        审阅发现 SpawnSubAgentTool 有 allowed_spawns 运行时校验，ParallelSpawnTool
        的 _run_one 却无条件构造 child——两工具不对称。此用例钉死：非 supervisor
        parent 的越界 spawn 必须 denied，且不构造 child（MockAgent 不被调用）。"""
        tool = ParallelSpawnTool()
        with patch("agents.supervisor.tools.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(return_value="ok")
            registry = make_mock_registry([tool])     # allowed_spawns 缺省 []
            agent = Agent(llm=make_mock_llm([]), agent_registry=registry,
                          agent_type="generate-note")
            result = tool.execute(spawns=[
                {"agent_type": "a", "task": "t1"},
                {"agent_type": "b", "task": "t2"},
            ])
        parsed = json.loads(result.text)
        assert len(parsed) == 2
        assert [p["status"] for p in parsed] == ["denied", "denied"]
        assert all("不能 spawn" in p["summary"] for p in parsed)
        MockAgent.assert_not_called()

    def test_parallel_resolve_timeout_map(self):
        """Parallel 与 Spawn 对称：同款 _resolve_timeout（R1 对齐哲学延续）。"""
        tool = ParallelSpawnTool(agent_timeouts={"generate-note": 300})
        assert tool._resolve_timeout("generate-note") == 300
        assert tool._resolve_timeout("other") == 120


class TestAggregateResultsTool:
    def test_marks_needs_attention(self):
        tool = AggregateResultsTool()
        result = tool.execute(results=[
            {"status": "success", "summary": "完成", "needs_attention": False},
            {"status": "denied", "summary": "写盘需确认", "needs_attention": True},
        ])
        assert "完成" in result.text
        assert "⚠️" in result.text
        assert "写盘需确认" in result.text


class TestAskUserTool:
    def test_callback_answer_becomes_result(self):
        tool = AskUserTool()
        calls = []
        def cb(q):
            calls.append(q)
            return "再搜索"
        tool._parent = MagicMock(ask_user_callback=cb)
        result = tool.execute(question="要搜索吗？")
        assert calls == ["要搜索吗？"]
        assert "再搜索" in result.text

    def test_failsafe_without_callback(self):
        tool = AskUserTool()
        tool._parent = MagicMock(ask_user_callback=None)
        result = tool.execute(question="要搜索吗？")
        assert "无法交互" in result.text


class TestStreamCallbackPropagation:
    def test_spawn_passes_stream_callback_to_child(self):
        """子 agent 继承父 stream_callback：单 spawn 全量流式的基础。"""
        cb = lambda ev: None
        with patch("agents.supervisor.tools.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(return_value="done")
            agent = _supervisor([SpawnSubAgentTool()], stream_callback=cb)
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="search-paper", task="搜索 x")
        kwargs = MockAgent.call_args.kwargs
        assert kwargs["stream_callback"] is cb
        assert "done" in json.loads(result.text)["summary"]

    def test_parallel_filters_content_and_prefixes_tool_events(self):
        """并行包装回调：content 丢弃、tool 加 [agent_type] 前缀（防多路 token 串字）。"""
        received = []
        parent_cb = received.append
        with patch("agents.supervisor.tools.Agent") as MockAgent:
            MockAgent.return_value.run = AsyncMock(return_value="ok")
            agent = _supervisor([ParallelSpawnTool()], stream_callback=parent_cb)
            agent.tools["parallel_spawn"].execute(spawns=[
                {"agent_type": "a", "task": "t1"},
            ])
        child_cb = MockAgent.call_args.kwargs["stream_callback"]
        child_cb(StreamEvent("content", "推理文本", "a"))              # content 被丢弃
        assert received == []
        child_cb(StreamEvent("tool", "调用 search_arxiv(query=x)", "a"))  # tool 加前缀透传
        assert received == [StreamEvent("tool", "[a] 调用 search_arxiv(query=x)", "a")]


# ─── 确认等待排除在超时外（2026-08-07 用户决策）───────────────────────
# 用户确认是交互等待，不应计入子 agent 的执行预算：预算 = 基础 timeout + 累积用户等待。
# 三件套：_UserWaitClock（begin/end 记录进行中等待）/ _wrap_confirm_callback（外包计时）/
# _run_child_with_budget（把累积等待加回剩余预算，确认期间超时暂停）。


class _ConfirmWriteTool(Tool):
    """requires_confirm=True 的写盘工具：触发 PolicyEngine ConfirmRequired → confirm_callback。

    与 test_cli.py 的 ConfirmWriteTool 同形态（本地定义，避免测试文件间耦合）。"""
    name = "confirm_write"
    description = "写盘，需确认"
    parameters = {"type": "object", "properties": {}}
    requires_confirm = True

    def execute(self) -> ToolResult:
        return ToolResult(text="written")


class TestUserWaitBudget:
    @pytest.mark.asyncio
    async def test_budget_excludes_user_wait(self):
        """0.1s 预算下：0.3s 确认等待（clock.begin/end 记录）+ 0.05s 执行 → 完成不超时。"""
        clock = _UserWaitClock()
        async def coro():
            clock.begin()                      # 等价 confirm wrapper：确认开始
            await asyncio.sleep(0.3)           # 模拟等待用户确认
            clock.end()                        # 等价 wrapper finally：确认结束
            await asyncio.sleep(0.05)          # 确认后的执行
            return "done"
        result = await _run_child_with_budget(coro(), timeout=0.1, clock=clock)
        assert result == "done"

    @pytest.mark.asyncio
    async def test_budget_times_out_without_user_wait(self):
        """纯执行超时仍触发：0.1s 预算下 0.5s 执行（无确认）→ TimeoutError（防误放行回归）。"""
        clock = _UserWaitClock()
        async def coro():
            await asyncio.sleep(0.5)
        with pytest.raises(asyncio.TimeoutError):
            await _run_child_with_budget(coro(), timeout=0.1, clock=clock)

    @pytest.mark.asyncio
    async def test_wrap_confirm_records_wait(self):
        """confirm wrapper 把等待时长记入 clock（进行中 + 结束时 total 都反映等待）。"""
        clock = _UserWaitClock()
        async def orig(cr):
            await asyncio.sleep(0.1)
            return True
        wrapped = _wrap_confirm_callback(orig, clock)
        assert await wrapped(None) is True
        assert clock.total() >= 0.1


class TestSpawnConfirmBudget:
    def test_confirm_wait_excluded_from_timeout(self):
        """集成：子 agent 写盘需确认（确认等待 0.2s > 预算 0.1s），确认后 success。

        回归锁死 2026-08-07 修复：若确认等待计入预算（旧 wait_for 纯墙钟），0.1s 预算下
        子 agent 在确认中途被误杀为 timeout；排除后预算延长，确认完成 → 正常 success。
        真实子 agent（真实 PolicyEngineMiddleware + confirm_callback 慢确认）经真实
        SpawnSubAgentTool._run_child 走完整链路。"""
        child_llm = make_mock_llm([
            _tc("confirm_write", {}),
            Message(role="assistant", content="写好了"),
        ])
        async def slow_confirm(cr):
            await asyncio.sleep(0.2)
            return True
        old = SpawnSubAgentTool.timeout
        SpawnSubAgentTool.timeout = 0.1
        try:
            agent = _supervisor(
                [SpawnSubAgentTool(), _ConfirmWriteTool()],
                llm=child_llm,
                security_middleware=[PolicyEngineMiddleware()],
                confirm_callback=slow_confirm,
            )
            result = agent.tools["spawn_sub_agent"].execute(
                agent_type="search-paper", task="写笔记")
        finally:
            SpawnSubAgentTool.timeout = old
        parsed = json.loads(result.text)
        assert parsed["status"] == "success"
        assert "写好了" in parsed["summary"]
