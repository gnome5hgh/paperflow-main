"""CLI REPL 测试：注入 fake io + 真实 renderer，验证循环/澄清挂起/超轮终止/EOF 路径。"""
import asyncio
import os
import signal

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from paperflow.cli import _repl, _make_confirm_callback, _make_ask_callback
from paperflow.terminal.io import FallbackIO
from paperflow.terminal.render import make_renderer
from paperflow.core.agent import Agent, MaxTurnsExceeded, StreamEvent
from paperflow.core.security import PolicyEngineMiddleware
from paperflow.core.llm import Message
from paperflow.core.intent.conversation_state import ConversationState
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.intent.schemas.intent import IntentType, IntentOutput, IntentStep
from tests.agent.test_agent import make_mock_registry, make_capture_llm


class ConfirmWriteTool(Tool):
    """requires_confirm=True 的写盘类工具：触发 PolicyEngine 的 ConfirmRequired 路径。

    （与 writer 的 WriteFileTool 同形态，真实 CLI 里靠 confirm_callback 放行。）"""
    name = "confirm_write"
    description = "写盘，需用户确认"
    parameters = {"type": "object", "properties": {}}
    requires_confirm = True

    def execute(self) -> ToolResult:
        return ToolResult(text="written")


def _seq_io(values: list[str]):
    """fake InputIO：按序返回 values，耗尽后 /exit。"""
    it = iter(values + ["/exit"])

    def read(prompt=""):
        try:
            return next(it)
        except StopIteration:
            return "/exit"
    return type("_FakeIO", (), {"read": staticmethod(read)})()


def _make_renderer(capture: list):
    """真实 StreamRenderer（非 TTY PlainBlock），输出捕获到 capture。"""
    return make_renderer(lambda *a, **k: capture.append(a[0]), "supervisor", is_tty=False)


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
    await _repl(sv, ConversationState(), io=_seq_io(["搜索 circRNA"]),
                renderer=_make_renderer(out))
    assert sv._calls == [("搜索 circRNA", False)]
    # 注：out 元素是整行文本（banner 含前缀），用子串匹配而非列表成员判定
    assert any("🌏" in s for s in out)
    assert "结果" in out


@pytest.mark.asyncio
async def test_repl_exit_only_no_run():
    sv = _make_supervisor([])
    await _repl(sv, ConversationState(), io=_seq_io([]), renderer=_make_renderer([]))
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
    conversation = ConversationState()
    out = []
    await _repl(sv, conversation, io=_seq_io(["搜索", "文献", "再答"]),
                renderer=_make_renderer(out))
    assert len(sv._calls) == 3
    assert sv._calls[0] == ("搜索", False)                    # T1 原输入
    assert sv._calls[1][0].startswith("搜索（用户澄清：文献）")  # T2 合并上下文
    assert sv._calls[1][1] is False
    assert sv._calls[2][1] is True                            # T3 超轮 force_dispatch
    assert sv._calls[2][0] == "搜索（用户澄清：文献）"          # best-guess 用累积上下文
    assert conversation.pending_intent is None                     # 超轮后清空
    assert "结果" in out


@pytest.mark.asyncio
async def test_repl_ctrl_d_exits_gracefully():
    """Ctrl-D（EOFError）与 /exit 同效，优雅退出不吐 traceback（🟠1）。"""
    sv = _make_supervisor([])

    class _EofIO:
        def read(self, prompt=""):
            raise EOFError
    await _repl(sv, ConversationState(), io=_EofIO(), renderer=_make_renderer([]))
    assert sv._calls == []


@pytest.mark.asyncio
async def test_repl_run_guard_max_turns_exceeded():
    """I1 回归：MaxTurnsExceeded 不杀 REPL——打印提示后 continue，能进入下一轮。

    D10 降级哲学：LLM 安全阀触发只报错不崩溃，REPL 存活可让用户换说法重试。
    _seq_io 会在耗尽后发 /exit，故循环必然正常退出（证明 continue 未破坏循环）。"""
    sv = MagicMock()
    async def run(query, force_dispatch=False):
        raise MaxTurnsExceeded("boom")
    sv.run = run
    sv.last_intent = None
    out = []
    await _repl(sv, ConversationState(), io=_seq_io(["搜索 x"]),
                renderer=_make_renderer(out))
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
    await _repl(sv, ConversationState(), io=_seq_io(["搜索 x"]),
                renderer=_make_renderer(out))
    assert any("执行出错：LLM 网络超时" in s for s in out)


def test_confirm_callback_eof_failsafe(monkeypatch):
    """确认回调 Ctrl-D → False（spec §5 fail-safe 承诺，沿用 C1 语义）。"""
    def _eof(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    cb = _make_confirm_callback(FallbackIO())
    assert asyncio.run(cb(SimpleNamespace(tool_name="write_file"))) is False


def test_confirm_callback_eof_tty_shim():
    """TTY 路径 EOF 兜底：io.confirm 抛 EOFError → 回调返回 False（两实现可替换）。

    FallbackIO.confirm 自捕 EOFError 返回 False；PromptToolkitIO 不捕——_confirm 里
    的 except EOFError 兜底分支只在后者触发，此处用 fake io 直接锁这条分支。"""
    class _EofConfirmIO:
        def confirm(self, text):
            raise EOFError
    cb = _make_confirm_callback(_EofConfirmIO())
    assert asyncio.run(cb(SimpleNamespace(tool_name="write_file"))) is False


def test_confirm_callback_ctrl_c_tty_shim():
    """TTY 路径 Ctrl+C 兜底：io.confirm 抛 KeyboardInterrupt → 回调返回 False（拒绝）。

    确认框里按 Ctrl+C，prompt_toolkit 的 c-c 键绑定在 worker 线程抛
    KeyboardInterrupt（经 to_thread 冒出）。不兜底会一路冲到 asyncio.run 崩 REPL
    ——deny 语义：Ctrl+C = 拒绝，与 EOF fail-safe 同效。"""
    class _KiConfirmIO:
        def confirm(self, text):
            raise KeyboardInterrupt
    cb = _make_confirm_callback(_KiConfirmIO())
    assert asyncio.run(cb(SimpleNamespace(tool_name="write_file"))) is False


@pytest.mark.asyncio
async def test_confirm_callback_async_contract_confirm(monkeypatch):
    """C1 回归（merge blocker）：confirm_callback（async）+ ConfirmRequired 不崩。

    agent.py:411 以 `await self.confirm_callback(cr)` 调用——若回调是 sync 的，`await True`
    抛 TypeError，writer 写盘工具（requires_confirm=True）在真实 CLI 永远写不出笔记。
    构造真实 Agent（真实 PolicyEngineMiddleware + async 确认回调），断言完整 ReAct
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
        confirm_callback=_make_confirm_callback(FallbackIO()),   # C1 修复点：async 回调
    )
    text = await agent.run("写笔记")
    assert text == "已写入"
    # 工具真实执行（y → 放行）：最后一轮 LLM 输入里含工具结果（当前 task 恒末位，
    # 工具结果在其前——按"在输入里"断言而非末位）
    assert any(m.role == "tool" and m.content == "written" for m in capture[-1])


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
        confirm_callback=_make_confirm_callback(FallbackIO()),
    )
    text = await agent.run("写笔记")
    assert text == "已取消"
    # 工具未执行（拒绝）：denial 结果在最后一轮 LLM 输入里（当前 task 恒末位，结果在其前）
    assert any(m.role == "tool" and "User denied: confirm_write" in m.content
               for m in capture[-1])


def test_ask_eof_returns_empty(monkeypatch):
    """ask_user 回调 Ctrl-D → 空串（Supervisor ReAct 自行处理）。"""
    def _eof(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert FallbackIO().ask("要哪个？") == ""


def test_ask_callback_eof_tty_shim():
    """TTY 路径 EOF 兜底：io.ask 抛 EOFError → 回调返回空串（两实现可替换）。

    FallbackIO.ask 自捕 EOFError 返回空串；PromptToolkitIO 不捕——_make_ask_callback
    里的 except EOFError 兜底分支只在后者触发，此处用 fake io 直接锁这条分支。"""
    class _EofAskIO:
        def ask(self, question):
            raise EOFError
    assert _make_ask_callback(_EofAskIO())("要哪个？") == ""


def test_ask_callback_ctrl_c_tty_shim():
    """TTY 路径 Ctrl+C 兜底：io.ask 抛 KeyboardInterrupt → 回调返回空串。

    提问框里按 Ctrl+C 与 EOF 同语义：中断输入返回空串，由 Supervisor ReAct 自行
    处理，不崩 REPL。"""
    class _KiAskIO:
        def ask(self, question):
            raise KeyboardInterrupt
    assert _make_ask_callback(_KiAskIO())("要哪个？") == ""


@pytest.mark.asyncio
async def test_repl_ctrl_c_during_run_interrupts_and_continues():
    """agent 运行中 Ctrl+C → 打印「已中断」、循环继续（不杀 REPL）。"""
    sv = MagicMock()
    async def run(query, force_dispatch=False):
        raise asyncio.CancelledError("cancelled")
    sv.run = run
    sv.last_intent = None
    out = []
    await _repl(sv, ConversationState(), io=_seq_io(["hi"]),
                renderer=_make_renderer(out))
    assert any("已中断" in s for s in out)


@pytest.mark.asyncio
async def test_repl_ctrl_c_sigint_cancels_run_and_continues():
    """真实 SIGINT 链路：OS 信号 → _cancel_run → task.cancel() → 已中断 → 循环继续。

    run 任务挂起在 sleep(30)；trigger 任务等 run 真正启动后 os.kill 发 SIGINT——这样
    信号必然落在已注册的 add_signal_handler 上（_repl 对不支持的环境降级，故先探测，
    不支持则 skip——该分支已有直接抛 CancelledError 的用例兜底）。"""
    loop = asyncio.get_running_loop()
    # 与 _repl 的 hasattr 探测一致：不支持的 loop（缺方法时调用会抛 AttributeError，
    # 不在 except 范围）直接 skip，而不是误报失败。
    if not (hasattr(loop, "add_signal_handler") and hasattr(loop, "remove_signal_handler")):
        pytest.skip("当前事件循环不支持 add_signal_handler")
    try:
        loop.add_signal_handler(signal.SIGINT, lambda: None)
        loop.remove_signal_handler(signal.SIGINT)
    except (NotImplementedError, RuntimeError):
        pytest.skip("当前事件循环不支持 add_signal_handler")

    sv = MagicMock()
    started = asyncio.Event()

    async def run(query, force_dispatch=False):
        started.set()
        await asyncio.sleep(30)
        return "结果"

    sv.run = run
    sv.last_intent = None

    async def _trigger():
        await started.wait()
        os.kill(os.getpid(), signal.SIGINT)

    out = []
    trigger = asyncio.create_task(_trigger())
    await _repl(sv, ConversationState(), io=_seq_io(["hi"]),
                renderer=_make_renderer(out))
    await trigger
    assert any("已中断" in s for s in out)


@pytest.mark.asyncio
async def test_repl_ctrl_c_on_input_exits():
    """输入框空、Ctrl+C → 退出（与 /exit、Ctrl-D 同效）。"""
    class _Fake:
        def read(self, prompt=""):
            raise KeyboardInterrupt
    sv = _make_supervisor([])
    await _repl(sv, ConversationState(), io=_Fake(), renderer=_make_renderer([]))
    assert sv._calls == []


@pytest.mark.asyncio
async def test_repl_streams_live_and_no_duplicate_final_print():
    """流式端到端：run() 经 sv.stream_callback 发 content 事件 → 增量实时打到
    renderer；最终答案已逐字展示 → 只补空行不重复打印（renderer 捕获每段）。"""
    sv = MagicMock()
    sv.agent_type = "supervisor"

    async def run(query, force_dispatch=False):
        sv.stream_callback(StreamEvent("content", "答", "supervisor"))
        sv.stream_callback(StreamEvent("content", "案", "supervisor"))
        return "答案"

    sv.run = run
    sv.last_intent = None
    out = []
    await _repl(sv, ConversationState(), io=_seq_io(["hi"]),
                renderer=_make_renderer(out))
    assert "答" in out and "案" in out          # 增量逐字捕获
    assert "" in out                            # should_print 返回 "" → 补换行
    assert not any(s == "答案" for s in out)    # 完整答案不重复打印
