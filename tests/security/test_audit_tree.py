# tests/security/test_audit_tree.py
"""调用树重建与跨日滚动测试。"""
import json
import pytest
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.llm import Message
from paperflow.core.security import ToolContext
from paperflow.core.security.audit import AuditMiddleware
from paperflow.core.tool import Tool, ToolResult
from tests.conftest import MockEchoTool


class ChildTool(Tool):
    """模拟 spawn：在 execute 内构造子 agent 并跑一次 run（to_thread 内 asyncio.run）。"""
    name = "child_tool"
    description = "runs a child"
    parameters = {"type": "object", "properties": {}}
    risk_level = "low"

    def __init__(self, child_agent_builder):
        self._builder = child_agent_builder

    def execute(self, **kwargs) -> ToolResult:
        child = self._builder()
        import asyncio
        text = asyncio.run(child.run("child task"))
        return ToolResult(text=text)


def read_entries(tmp_path):
    return [json.loads(l) for l in next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()]


@pytest.mark.asyncio
async def test_child_events_nest_under_parent_spawn(tmp_path):
    """父子链：子 agent 的工具调用 parent_id == 父 spawn 的 span_id，depth=父+1。"""
    mw = AuditMiddleware(audit_dir=str(tmp_path))

    def make_llm(responses):
        mock = MagicMock()
        async def chat(messages, tools=None, tool_choice="auto", telemetry_callback=None):
            return responses.pop(0) if responses else Message(role="assistant", content="done")
        mock.chat = chat
        return mock

    # 注册表 mock：按 agent_type 分发的 side_effect 函数（不能用"列表耗尽"模式——
    # 列表耗尽抛 StopIteration，经 asyncio Future 传播会触发
    # "StopIteration interacts badly with generators" 使事件循环卡死）。
    registry = MagicMock(spec=AgentRegistry)
    child_cfg: dict = {}

    def fake_get_config(agent_type):
        return child_cfg["cfg"] if agent_type == "child" else parent_cfg

    # 子 agent 首轮 mock 要求调 echo（产生一对子 tool_started/tool_ended），第二轮起返回文本结束
    def build_child():
        child_cfg["cfg"] = AgentConfig(
            name="child", system_prompt="p", tools=[MockEchoTool()])
        echo_call = Message(
            role="assistant", content=None,
            tool_calls=[{"id": "c1", "type": "function",
                         "function": {"name": "echo", "arguments": '{"message": "hi"}'}}])
        return Agent(llm=make_llm([echo_call]), agent_registry=registry, agent_type="child",
                     security_middleware=[mw])

    parent_cfg = AgentConfig(name="parent", system_prompt="p", tools=[ChildTool(build_child)])
    registry.get_config.side_effect = fake_get_config
    parent = Agent(llm=make_llm([]), agent_registry=registry, agent_type="parent",
                   security_middleware=[mw])
    # 首轮 LLM 要求调 child_tool；child 内部调一次 echo 后返回文本
    await parent._exec_tool(
        {"id": "c1", "type": "function",
         "function": {"name": "child_tool", "arguments": "{}"}})

    events = read_entries(tmp_path)
    by_type = {}
    for e in events:
        by_type.setdefault(e["event_type"], []).append(e)
    # 子事件在父 after 弹栈**之前**写盘（child 跑在 execute 内），故按工具名挑
    # tool_ended 事件（span 收口带完整父链），不依赖写盘顺序
    spawn = [e for e in by_type["tool_ended"] if e["tool_name"] == "child_tool"][0]
    child_echo = [e for e in by_type["tool_ended"] if e["tool_name"] == "echo"][0]
    assert child_echo["parent_id"] == spawn["span_id"]    # 子事件挂在 spawn 下
    assert child_echo["depth"] == spawn["depth"] + 1


def test_rollover_resolves_path_per_write(tmp_path):
    """跨日滚动：路径按写盘当天解析（不再构造时固定）。"""
    mw = AuditMiddleware(audit_dir=str(tmp_path))
    assert mw.audit_dir == str(tmp_path)
    ctx = ToolContext(trace_id="t", session_id="s", agent_type="a",
                      tool_name="x", args={})
    import asyncio

    async def drive():
        await mw.before(ctx)   # 压栈（真实 token），保证 after 的弹栈路径可测
        await mw.after(ctx)    # 按写盘当天解析文件名并追加写入

    asyncio.run(drive())
    from datetime import datetime
    expected = tmp_path / f"audit_{datetime.now():%Y%m%d}.jsonl"
    assert expected.exists()
