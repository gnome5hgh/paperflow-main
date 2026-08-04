"""Supervisor 4 个调度工具测试（mock child，无真实 LLM）。"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.tool import ToolResult
from tests.test_agent import make_mock_llm, make_mock_registry

from agents.supervisor.tools import (
    SpawnSubAgentTool, ParallelSpawnTool, AggregateResultsTool, AskUserTool,
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
        finally:
            SpawnSubAgentTool.timeout = old
        parsed = json.loads(result.text)
        assert parsed["status"] == "timeout"
        assert parsed["error_detail"] == "SubAgent 在 120s 内未完成"

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
