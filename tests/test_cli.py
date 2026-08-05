"""CLI REPL 测试：注入 input_fn/print_fn，验证循环/澄清挂起/超轮终止/EOF 路径。"""
import asyncio

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from paperflow.cli import _repl, _stdin_confirm, _stdin_ask
from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.security import PolicyEngineMiddleware
from paperflow.core.llm import Message
from paperflow.core.session import Session
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.intent.intent_schema import IntentType, IntentOutput, IntentStep
from tests.test_agent import make_mock_registry, make_capture_llm


class ConfirmWriteTool(Tool):
    """requires_confirm=True 的写盘类工具：触发 PolicyEngine 的 ConfirmRequired 路径。

    （与 generate-note 的 WriteFileTool 同形态，真实 CLI 里靠 confirm_callback 放行。）"""
    name = "confirm_write"
    description = "写盘，需用户确认"
    parameters = {"type": "object", "properties": {}}
    requires_confirm = True

    def execute(self) -> ToolResult:
        return ToolResult(text="written")


def _seq_input(values: list[str]):
    """同步 input_fn（_repl 里 input_fn 是同步调用）：按序返回 values，耗尽后 /exit。"""
    it = iter(values + ["/exit"])
    def _fn(prompt=""):
        try:
            return next(it)
        except StopIteration:
            return "/exit"
    return _fn


def _make_supervisor(intent_sequence):
    """mock Supervisor：run 记录调用、每次 run 后按序设置 last_intent。"""
    sv = MagicMock()
    calls = []
    async def run(query, force_dispatch=False):
        calls.append((query, force_dispatch))
        if intent_sequence:
            sv.last_intent = intent_sequence.pop(0)
        return "结果"
    sv.run = run
    sv.last_intent = None
    sv._calls = calls
    return sv


@pytest.mark.asyncio
async def test_repl_prints_result_and_exits():
    sv = _make_supervisor([None])
    out = []
    await _repl(sv, Session(), input_fn=_seq_input(["搜索 circRNA"]),
                print_fn=lambda *a: out.append(a[0]))
    assert sv._calls == [("搜索 circRNA", False)]
    # 注：out 元素是整行文本（banner 含前缀），用子串匹配而非列表成员判定
    assert any("🌏" in s for s in out)
    assert "结果" in out


@pytest.mark.asyncio
async def test_repl_exit_only_no_run():
    sv = _make_supervisor([])
    await _repl(sv, Session(), input_fn=_seq_input([]),
                print_fn=lambda *a: None)
    assert sv._calls == []


@pytest.mark.asyncio
async def test_clarification_suspends_then_terminates():
    """T1 挂起 round=1 → T2 再澄清 round=2（链式不重置）→ T3 超轮 force_dispatch 终止。

    三次 run 的调用序列即轮数链式的证据：第三次 force=True 说明 merge 看到 round>=2
    （若 round 被重置为 0，第三次不会强制——防死循环关键，spec D9）。
    """
    sv = _make_supervisor([
        IntentOutput(intent_type=IntentType.ASK_QUESTION, confidence=0.5,
                     source=IntentStep.LLM, clarification="要搜索还是生成？"),
        IntentOutput(intent_type=IntentType.ASK_QUESTION, confidence=0.5,
                     source=IntentStep.LLM, clarification="仍不明确？"),
        None,                                        # 第三次：不再挂起，直接出结果
    ])
    session = Session()
    out = []
    await _repl(sv, session, input_fn=_seq_input(["搜索", "文献", "再答"]),
                print_fn=lambda *a: out.append(a[0]))
    assert len(sv._calls) == 3
    assert sv._calls[0] == ("搜索", False)                    # T1 原输入
    assert sv._calls[1][0].startswith("搜索（用户澄清：文献）")  # T2 合并上下文
    assert sv._calls[1][1] is False
    assert sv._calls[2][1] is True                            # T3 超轮 force_dispatch
    assert sv._calls[2][0] == "搜索（用户澄清：文献）"          # best-guess 用累积上下文
    assert session.pending_intent is None                     # 超轮后清空
    assert "结果" in out


@pytest.mark.asyncio
async def test_repl_ctrl_d_exits_gracefully():
    """Ctrl-D（EOFError）与 /exit 同效，优雅退出不吐 traceback（🟠1）。"""
    sv = _make_supervisor([])
    def _eof_input(prompt=""):
        raise EOFError
    await _repl(sv, Session(), input_fn=_eof_input, print_fn=lambda *a: None)
    assert sv._calls == []


@pytest.mark.asyncio
async def test_repl_run_guard_max_turns_exceeded():
    """I1 回归：MaxTurnsExceeded 不杀 REPL——打印提示后 continue，能进入下一轮。

    D10 降级哲学：LLM 安全阀触发只报错不崩溃，REPL 存活可让用户换说法重试。
    _seq_input 会在耗尽后发 /exit，故循环必然正常退出（证明 continue 未破坏循环）。"""
    sv = MagicMock()
    async def run(query, force_dispatch=False):
        raise MaxTurnsExceeded("boom")
    sv.run = run
    sv.last_intent = None
    out = []
    await _repl(sv, Session(), input_fn=_seq_input(["搜索 x"]),
                print_fn=lambda *a: out.append(a[0]))
    assert any("任务超过最大轮数" in s for s in out)


@pytest.mark.asyncio
async def test_repl_run_guard_generic_exception():
    """I1 回归：未预期异常（如 LLM 网络失败）不杀 REPL——打印错误、continue。"""
    sv = MagicMock()
    async def run(query, force_dispatch=False):
        raise RuntimeError("LLM 网络超时")
    sv.run = run
    sv.last_intent = None
    out = []
    await _repl(sv, Session(), input_fn=_seq_input(["搜索 x"]),
                print_fn=lambda *a: out.append(a[0]))
    assert any("执行出错：LLM 网络超时" in s for s in out)


def test_stdin_confirm_eof_failsafe(monkeypatch):
    """confirm 回调 Ctrl-D → False（spec §6.3 fail-safe 承诺，🟠1）。

    C1 后 _stdin_confirm 是 async——同步调用拿到的是 coroutine 而非 bool，
    必须 asyncio.run 包一层（回调本身跑在 Agent 的事件循环里，_repl 外是 sync 测试）。"""
    def _eof(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert asyncio.run(_stdin_confirm(SimpleNamespace(tool_name="write_file"))) is False


@pytest.mark.asyncio
async def test_confirm_callback_async_contract_confirm(monkeypatch):
    """C1 回归（merge blocker）：confirm_callback=_stdin_confirm（async）+ ConfirmRequired 不崩。

    agent.py:411 以 `await self.confirm_callback(cr)` 调用——若回调是 sync 的，`await True`
    抛 TypeError，generate-note 写盘工具（requires_confirm=True）在真实 CLI 永远写不出笔记。
    构造真实 Agent（真实 PolicyEngineMiddleware + async _stdin_confirm），断言完整 ReAct
    走通：输入 y → 确认放行 → 工具执行 → 返回最终答案，全程无 TypeError。
    """
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    capture = []
    llm = make_capture_llm([
        Message(role="assistant", content=None, tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "confirm_write", "arguments": "{}"},
        }]),
        Message(role="assistant", content="已写入"),
    ], capture)
    agent = Agent(
        llm=llm, agent_registry=make_mock_registry([ConfirmWriteTool()]),
        agent_type="test",
        security_middleware=[PolicyEngineMiddleware()],
        confirm_callback=_stdin_confirm,          # C1 修复点：async 回调
    )
    text = await agent.run("写笔记")
    assert text == "已写入"
    # 工具真实执行（y → 放行）：最后一轮 LLM 输入里含工具结果
    assert capture[-1][-1].content == "written"


@pytest.mark.asyncio
async def test_confirm_callback_async_contract_denied(monkeypatch):
    """C1 补充：确认回调返回 False（用户拒绝）→ 不执行工具，返回 user denied，不抛 TypeError。"""
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    capture = []
    llm = make_capture_llm([
        Message(role="assistant", content=None, tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "confirm_write", "arguments": "{}"},
        }]),
        Message(role="assistant", content="已取消"),
    ], capture)
    agent = Agent(
        llm=llm, agent_registry=make_mock_registry([ConfirmWriteTool()]),
        agent_type="test",
        security_middleware=[PolicyEngineMiddleware()],
        confirm_callback=_stdin_confirm,
    )
    text = await agent.run("写笔记")
    assert text == "已取消"
    assert "User denied: confirm_write" in capture[-1][-1].content  # 工具未执行


def test_stdin_ask_eof_returns_empty(monkeypatch):
    """ask_user 回调 Ctrl-D → 空串（Supervisor ReAct 自行处理，🟠1）。"""
    def _eof(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert _stdin_ask("要哪个？") == ""


from paperflow.cli import _ReplStreamer
from paperflow.core.agent import StreamEvent


def _collect():
    """返回 (out, print_fn)：print_fn 兼容 end=/flush= kwargs，捕获每次调用首参。"""
    out = []

    def _fn(*a, **k):
        out.append(a[0])
    return out, _fn


class TestReplStreamer:
    def test_content_segments_insert_newlines_on_transition(self):
        out, fn = _collect()
        s = _ReplStreamer(fn, root_agent_type="supervisor")
        s.on_event(StreamEvent("content", "答", "supervisor"))
        s.on_event(StreamEvent("content", "案", "supervisor"))
        s.on_event(StreamEvent("content", "推理", "search-paper"))   # root → child
        s.on_event(StreamEvent("content", "续", "search-paper"))
        s.on_event(StreamEvent("content", "总结", "supervisor"))     # child → root
        assert "".join(out) == "答案\n推理续\n总结"

    def test_root_tool_event_clears_buffer(self):
        out, fn = _collect()
        s = _ReplStreamer(fn, "supervisor")
        s.on_event(StreamEvent("content", "中间想法", "supervisor"))
        s.on_event(StreamEvent("tool", "调用 search_paper(query=x)", "supervisor"))
        assert s.should_print("最终答案") == "最终答案"    # buffer 被清 → 走现状
        s.on_event(StreamEvent("content", "最终答案", "supervisor"))
        assert s.should_print("最终答案") == ""            # 已逐字展示 → 只补换行

    def test_should_print_rewrite_case(self):
        out, fn = _collect()
        s = _ReplStreamer(fn, "supervisor")
        s.on_event(StreamEvent("content", "原始内容", "supervisor"))
        assert s.should_print("SAFE_PROMPT") == "\nSAFE_PROMPT"   # on_finish 改写 → 补打

    def test_child_content_does_not_pollute_buffer(self):
        out, fn = _collect()
        s = _ReplStreamer(fn, "supervisor")
        s.on_event(StreamEvent("content", "子agent回答", "search-paper"))   # child 不入 buffer
        assert s.should_print("最终答案") == "最终答案"
        s.on_event(StreamEvent("content", "最终答案", "supervisor"))
        assert s.should_print("最终答案") == ""

    def test_reset_clears_stale_buffer(self):
        out, fn = _collect()
        s = _ReplStreamer(fn, "supervisor")
        s.on_event(StreamEvent("content", "残留", "supervisor"))
        s.reset()
        assert s.should_print("结果") == "结果"           # 残留被清 → 走现状

    def test_no_double_newline_with_real_print_behavior(self):
        """回归：真实 print 默认 end="\\n"，_print("\\n") 必须传 end="" 否则多出空行。"""
        out = []
        def _fn(*a, **k):
            out.append(a[0] + k.get("end", "\n"))
        s = _ReplStreamer(_fn, "supervisor")
        s.on_event(StreamEvent("content", "答", "supervisor"))
        s.on_event(StreamEvent("content", "推理", "search-paper"))   # root→child 段切换
        s.on_event(StreamEvent("tool", "调用 search_arxiv(query=x)", "search-paper"))
        joined = "".join(out)
        assert "\n\n" not in joined
        assert joined == "答\n推理\n调用 search_arxiv(query=x)\n"


@pytest.mark.asyncio
async def test_repl_streams_live_and_no_duplicate_final_print():
    """流式端到端：run() 经 sv.stream_callback 发 content 事件 → 增量实时打到
    print_fn；最终答案已逐字展示 → 只补空行不重复打印（print_fn 需兼容 kwargs）。"""
    sv = MagicMock()
    sv.agent_type = "supervisor"

    async def run(query, force_dispatch=False):
        sv.stream_callback(StreamEvent("content", "答", "supervisor"))
        sv.stream_callback(StreamEvent("content", "案", "supervisor"))
        return "答案"

    sv.run = run
    sv.last_intent = None
    out = []
    await _repl(sv, Session(), input_fn=_seq_input(["hi"]),
                print_fn=lambda *a, **k: out.append(a[0]))
    assert "答" in out and "案" in out          # 增量逐字捕获
    assert "" in out                            # should_print 返回 "" → 补换行
    assert not any(s == "答案" for s in out)    # 完整答案不重复打印
