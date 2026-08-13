"""原子写共享模块测试：临时文件 + os.replace，失败不留残留。"""
import os
from pathlib import Path

from paperflow.tools.file.atomic import atomic_write


def test_atomic_write_replaces_content(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("old", encoding="utf-8")
    atomic_write(target, "new content")
    assert target.read_text(encoding="utf-8") == "new content"


def test_atomic_write_creates_missing_parent(tmp_path):
    target = tmp_path / "a" / "b" / "note.md"
    atomic_write(target, "hi")
    assert target.read_text(encoding="utf-8") == "hi"


def test_atomic_write_leaves_no_temp(tmp_path):
    target = tmp_path / "note.md"
    atomic_write(target, "x")
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_atomic_write_uses_os_replace(monkeypatch, tmp_path):
    """原子写必须经 os.replace——非 write_text 直接覆盖（防部分写入）。"""
    target = tmp_path / "note.md"
    calls = []
    real_replace = os.replace
    monkeypatch.setattr("paperflow.tools.file.atomic.os.replace",
                        lambda a, b: calls.append((a, b)) or real_replace(a, b))
    atomic_write(target, "data")
    assert calls and calls[0][1] == str(target)   # 目标路径是 replace 的 dst
