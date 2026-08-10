# tests/security/test_audit_tree.py
"""调用树重建、跨日滚动与父链不变量测试。"""
import json
import pytest
from unittest.mock import MagicMock

from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentConfig, AgentRegistry
from paperflow.core.llm import Message
from paperflow.core.security import ToolContext
from paperflow.core.security.audit import AuditMiddleware
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
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


def make_llm(responses):
    """构造按序吐出给定响应序列的 mock LLM；序列耗尽后返回默认结束消息。"""
    mock = MagicMock()

    async def chat(messages, tools=None, tool_choice="auto", telemetry_callback=None):
        return responses.pop(0) if responses else Message(role="assistant", content="done")
    mock.chat = chat
    return mock


def read_entries(tmp_path):
    return [json.loads(l) for l in next(tmp_path.glob("audit_*.jsonl")).read_text().strip().splitlines()]


async def run_parent_child(mw):
    """跑一遍真实父→子链：父 agent 调 child_tool，子 agent 内部调一次 echo。
    所有审计事件写入 mw 的 audit 目录，供后续按不变量扫描。"""
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


def assert_no_orphans(entries):
    """树不变量扫描：每个非根 parent_id 都必须在文件里有对应 tool_started。
    违反即「子事件引用了不存在的父 span」——中断的子树被误写成孤儿（P1 修复核心）。"""
    started_ids = {e["span_id"] for e in entries if e["event_type"] == "tool_started"}
    for e in entries:
        if e["parent_id"] is not None:
            assert e["parent_id"] in started_ids, (
                f"{e['event_type']} span={e['span_id']} 引用了不存在的父 span={e['parent_id']}")


@pytest.mark.asyncio
async def test_child_events_nest_under_parent_spawn(tmp_path):
    """父子链：子 agent 的工具调用 parent_id == 父 spawn 的 span_id，depth=父+1。"""
    mw = AuditMiddleware(audit_dir=str(tmp_path))
    await run_parent_child(mw)

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


@pytest.mark.asyncio
async def test_no_orphan_invariant(tmp_path):
    """父链不孤儿：跑完真实父→子链后扫描全文件，每个非根 parent_id 都有对应 tool_started。"""
    mw = AuditMiddleware(audit_dir=str(tmp_path))
    await run_parent_child(mw)
    assert_no_orphans(read_entries(tmp_path))


@pytest.mark.asyncio
async def test_no_orphan_scan_rejects_dangling_parent(tmp_path):
    """不变量扫描非空：向真实审计文件注入一个悬空父引用后，扫描必须拒绝。
    防止扫描退化为恒真（对任何数据都通过）。"""
    mw = AuditMiddleware(audit_dir=str(tmp_path))
    await run_parent_child(mw)
    # 注入一条引用不存在父 span 的事件，模拟 P1 修复前的孤儿输出
    orphan = {"event_type": "tool_ended", "span_id": "span_c", "parent_id": "span_ghost"}
    path = next(tmp_path.glob("audit_*.jsonl"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(orphan) + "\n")
    with pytest.raises(AssertionError):
        assert_no_orphans(read_entries(tmp_path))


def test_interrupted_span_has_start_no_end(tmp_path):
    """中断语义：只写 before（tool_started）不写 after 的 span，可识别为中断而非悬空引用。"""
    mw = AuditMiddleware(audit_dir=str(tmp_path))
    ctx = ToolContext(trace_id="t", session_id="s", agent_type="a", tool_name="x", args={})
    import asyncio
    asyncio.run(mw.before(ctx))   # 只落 tool_started，不落 tool_ended——模拟中断
    entries = read_entries(tmp_path)
    started = [e for e in entries
               if e["event_type"] == "tool_started" and e["span_id"] == ctx.span_id]
    ended = [e for e in entries
             if e["event_type"] == "tool_ended" and e["span_id"] == ctx.span_id]
    assert len(started) == 1
    assert ended == []    # 无配对 tool_ended = 中断（可识别），而非子事件引用的悬空父


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


class _DigestE2ELLM:
    """mock LLM：ReAct 调用按序返回给定响应；摘要提取调用（json_mode=True）触发 telemetry 回调。

    StructuredOutput.extract 以 json_mode=True 调 chat——这是识别「摘要提取调用」的稳定信号
    （Agent 的 ReAct chat 恒为 json_mode=False）。telemetry 回调必须在摘要调用里真实触发，
    否则 parent._emit_llm_call 收不到数据，审计里不会有那条 llm_call——锁住 spawn.py 的
    回调接线。
    """

    def __init__(self, responses):
        self.responses = list(responses)

    async def chat(self, messages, tools=None, tool_choice="auto", json_mode=False,
                   temperature=None, extra_body=None, telemetry_callback=None):
        if telemetry_callback is not None:
            telemetry_callback({
                "model": "mock", "prompt_tokens": 10, "completion_tokens": 5,
                "total_tokens": 15, "duration_ms": 100,
                "started_at": "2026-08-09T00:00:00", "finish_reason": "stop"})
        if json_mode:
            return Message(role="assistant", content=json.dumps({
                "count": 3, "papers": ["p1", "p2"], "needs_attention": False}))
        return self.responses.pop(0) if self.responses else Message(role="assistant", content="done")


@pytest.mark.asyncio
async def test_spawn_digest_llm_call_attrs_to_parent(tmp_path):
    """digest 端到端（P5 核心）：真实 spawn 路径的摘要 LLM 调用写 llm_call，归父 trace/轮次。

    走完整真实链：parent.run → SpawnSubAgentTool._run_child → _extract_digest →
    StructuredOutput → llm.chat（json_mode=True）→ telemetry 回调 → parent._emit_llm_call。
    断言该 llm_call 的 parent_id == 该 spawn 的 span_id、turn == 父 spawn 所在轮次、
    agent_type == 父的 agent_type——锁住 spawn.py 里那条 lambda：它断了，这条事件就没了，
    而各环节单元测试仍会静默通过。
    """
    mw = AuditMiddleware(audit_dir=str(tmp_path))
    registry = MagicMock(spec=AgentRegistry)
    parent_cfg = AgentConfig(name="supervisor", system_prompt="p",
                             tools=[SpawnSubAgentTool()])
    child_cfg = AgentConfig(name="searcher", system_prompt="p", tools=[MockEchoTool()])
    registry.get_config.side_effect = (
        lambda at: parent_cfg if at == "supervisor" else child_cfg)

    parent = Agent(
        llm=_DigestE2ELLM([
            # 父第一轮：要求 spawn 一个 searcher
            Message(role="assistant", content=None, tool_calls=[
                {"id": "c1", "type": "function",
                 "function": {"name": "spawn_sub_agent",
                              "arguments": json.dumps(
                                  {"agent_type": "searcher", "task": "research x"})}}]),
            # 子 agent 的回答（同一 mock llm，json_mode=False 时消费）
            Message(role="assistant", content="child done"),
            # 父第二轮：结束
            Message(role="assistant", content="parent done"),
        ]),
        agent_registry=registry, agent_type="supervisor",
        security_middleware=[mw])
    out = await parent.run("research x")
    assert out == "parent done"

    events = read_entries(tmp_path)
    assert_no_orphans(events)                     # 树不变量：digest 挂 spawn 下，不孤儿
    by_type = {}
    for e in events:
        by_type.setdefault(e["event_type"], []).append(e)
    spawn_started = [e for e in by_type["tool_started"]
                     if e["tool_name"] == "spawn_sub_agent"][0]
    # 只挑「parent_id == spawn span 且 agent_type == 父」的 llm_call——摘要提取那条；
    # 子 agent 自己的 llm_call agent_type 是 searcher，父 ReAct 的 llm_call 无 parent
    digest_calls = [e for e in by_type["llm_call"]
                    if e["parent_id"] == spawn_started["span_id"]
                    and e["agent_type"] == parent.agent_type]
    assert len(digest_calls) == 1
    digest_call = digest_calls[0]
    assert digest_call["turn"] == spawn_started["turn"]          # 归父 spawn 所在轮次
    assert digest_call["depth"] == spawn_started["depth"] + 1    # 摘要嵌在 spawn 内一层
