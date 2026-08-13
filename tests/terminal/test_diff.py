# tests/terminal/test_diff.py
"""diff 计算/截断共享模块测试。"""
from paperflow.terminal.diff import compute_diff, truncate_diff


def test_compute_diff_adds_and_removes():
    old = "line1\nline2\nline3"
    new = "line1\nline2 changed\nline3\nline4"
    d = compute_diff(old, new, fromfile="a.md", tofile="a.md")
    assert "--- a.md" in d and "+++ a.md" in d
    assert "-line2" in d and "+line2 changed" in d and "+line4" in d
    assert "line1" in d and "line3" in d          # 上下文保留


def test_compute_diff_empty_old_is_all_additions():
    d = compute_diff("", "new\ncontent", fromfile="a.md", tofile="a.md")
    assert "+new" in d and "+content" in d


def test_truncate_diff_caps_lines():
    diff = "\n".join(f"line{i}" for i in range(300))
    t = truncate_diff(diff, max_lines=200)
    assert len(t.splitlines()) == 201              # 200 行 + 1 行省略标记
    assert "… +100 lines" in t
    # 未超限原样返回
    assert truncate_diff("abc", max_lines=200) == "abc"
