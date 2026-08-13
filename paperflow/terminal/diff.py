# paperflow/terminal/diff.py
"""统一 diff 计算与显示截断。

确认预览（cli 确认回调）与写后展示共用：difflib.unified_diff 算差异、按行数截断
超大 diff，避免终端被刷屏。纯函数、无 IO，测试可直驱。
"""
import difflib


def compute_diff(old_text: str, new_text: str, fromfile: str = "old", tofile: str = "new") -> str:
    """unified diff 文本（-3/+3 上下文）。old 为空 = 新文件全新增。"""
    old_lines = old_text.splitlines() if old_text else []
    new_lines = new_text.splitlines() if new_text else []
    return "\n".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=fromfile, tofile=tofile, lineterm=""))


def truncate_diff(diff: str, max_lines: int = 200) -> str:
    """超大 diff 显示截断：前 max_lines 行 + `… +N lines`；未超限原样返回。"""
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return diff
    return "\n".join(lines[:max_lines] + [f"… +{len(lines) - max_lines} lines"])
