# tests/terminal/test_io.py
from unittest.mock import MagicMock

import pytest
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.history import FileHistory
from prompt_toolkit.keys import Keys

from paperflow.config import PaperFlowConfig
from paperflow.terminal.io import (
    FallbackIO, InputIO, PromptToolkitIO, _session_key_bindings, make_input_io,
)


def test_fallback_read_returns_input(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda prompt="": "搜索 circRNA")
    assert FallbackIO().read("> ") == "搜索 circRNA"


def test_fallback_confirm_accepts_yes(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    assert FallbackIO().confirm("是否继续？") is True


def test_fallback_confirm_rejects_no(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
    assert FallbackIO().confirm("是否继续？") is False


def test_fallback_confirm_eof_failsafe(monkeypatch):
    def _eof(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert FallbackIO().confirm("是否继续？") is False


def test_fallback_ask_eof_returns_empty(monkeypatch):
    def _eof(*a, **k):
        raise EOFError
    monkeypatch.setattr("builtins.input", _eof)
    assert FallbackIO().ask("要哪个？") == ""


def test_prompt_toolkit_read_delegates_to_session():
    fake = MagicMock()
    fake.prompt.return_value = "query"
    io = PromptToolkitIO("x", session=fake)
    assert io.read("> ") == "query"
    fake.prompt.assert_called_once_with("> ")


def test_prompt_toolkit_constructs_history_and_autosuggest(tmp_path):
    history_path = tmp_path / "repl_history.txt"
    io = PromptToolkitIO(str(history_path), session=None)
    session = io._session
    # 多行编辑 + Enter 提交 + 历史落盘 + 自动建议（契约级断言）
    assert session.multiline is True
    assert isinstance(session.history, FileHistory)
    assert isinstance(session.auto_suggest, AutoSuggestFromHistory)


def test_make_input_io_non_tty_returns_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    cfg = PaperFlowConfig()
    cfg.workspace = str(tmp_path)
    io = make_input_io(cfg)
    assert isinstance(io, FallbackIO)


def test_make_input_io_tty_returns_prompt_toolkit(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    cfg = PaperFlowConfig()
    cfg.workspace = str(tmp_path)
    io = make_input_io(cfg)
    assert isinstance(io, PromptToolkitIO)


def _binding(*keys):
    """按键元组从 _session_key_bindings 中取绑定（每个键恰好一个绑定）。"""
    kb = _session_key_bindings()
    return next(b for b in kb.bindings if list(b.keys) == list(keys))


def _event(text=""):
    ev = MagicMock()
    ev.current_buffer.text = text
    return ev


def test_binding_enter_submits():
    b = _binding(Keys.ControlM)
    ev = _event()
    b.handler(ev)
    ev.current_buffer.validate_and_handle.assert_called_once_with()


def test_binding_alt_enter_inserts_newline():
    b = _binding(Keys.Escape, Keys.ControlM)
    ev = _event()
    b.handler(ev)
    ev.current_buffer.insert_text.assert_called_once_with("\n")


def test_binding_ctrl_c_with_text_clears():
    b = _binding(Keys.ControlC)
    ev = _event(text="abc")
    b.handler(ev)
    ev.current_buffer.reset.assert_called_once_with()


def test_binding_ctrl_c_empty_raises_keyboard_interrupt():
    b = _binding(Keys.ControlC)
    with pytest.raises(KeyboardInterrupt):
        b.handler(_event())


def test_binding_ctrl_d_empty_raises_eof():
    b = _binding(Keys.ControlD)
    with pytest.raises(EOFError):
        b.handler(_event())


def test_prompt_toolkit_confirm_bindings_toggle_and_accept():
    """confirm 选择器键绑定：←/→ 切换选择、Enter 以所选结果退出（结构性测试，真 TTY 人工冒烟）。

    state 初始 False = 默认选 No（与 FallbackIO 的 (y/N) 默认拒绝一致）。"""
    from paperflow.terminal.io import _confirm_key_bindings
    state = [False]
    kb = _confirm_key_bindings(state)
    binds = {tuple(b.keys): b for b in kb.bindings}
    assert (Keys.Left,) in binds and (Keys.Right,) in binds and (Keys.Enter,) in binds

    # ←/→ 翻转 state 闭包
    ev = MagicMock(); ev.current_buffer = MagicMock()
    binds[(Keys.Right,)].handler(ev)
    assert state == [True]
    binds[(Keys.Left,)].handler(ev)
    assert state == [False]

    # Enter → app.exit(result=state[0])——切到 Yes 再 Enter 放行
    binds[(Keys.Right,)].handler(ev)
    ev2 = MagicMock(); ev2.app = MagicMock()
    binds[(Keys.Enter,)].handler(ev2)
    ev2.app.exit.assert_called_once_with(result=True)


def test_fallback_confirm_keeps_text_y_n(monkeypatch, capsys):
    """FallbackIO 确认文案带 (y/N) 提示（英文兜底）。"""
    monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
    FallbackIO().confirm("Continue?")
    assert "(y/N)" in capsys.readouterr().out
