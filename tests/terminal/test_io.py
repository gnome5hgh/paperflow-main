# tests/terminal/test_io.py
from paperflow.terminal.io import FallbackIO, InputIO


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
