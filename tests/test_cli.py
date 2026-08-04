"""CLI REPL 测试：注入 input_fn/print_fn，验证循环/澄清挂起/超轮终止/EOF 路径。"""
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock

from paperflow.cli import _repl, _stdin_confirm, _stdin_ask
from paperflow.core.session import Session
from paperflow.core.intent.intent_schema import IntentType, IntentOutput, IntentStep


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


def test_stdin_confirm_eof_failsafe(monkeypatch):
    """confirm 回调 Ctrl-D → False（spec §6.3 fail-safe 承诺，🟠1）。"""
    def _eof(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert _stdin_confirm(SimpleNamespace(tool_name="write_file")) is False


def test_stdin_ask_eof_returns_empty(monkeypatch):
    """ask_user 回调 Ctrl-D → 空串（Supervisor ReAct 自行处理，🟠1）。"""
    def _eof(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert _stdin_ask("要哪个？") == ""
