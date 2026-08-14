# paperflow/terminal/diff.py
"""统一 diff 计算与显示截断。

写/编辑工具的确认预览与展示共用：difflib.unified_diff 算差异、按行数截断超大
diff，避免终端被刷屏。纯函数、无 IO，测试可直驱。
"""
import difflib


def compute_diff(old_text: str, new_text: str, fromfile: str = "old", tofile: str = "new") -> str:
    """算 unified diff（-3/+3 上下文），返回多行文本。

    old_text 为空视为新文件全新增。fromfile/tofile 是 diff 头里的文件标签（同一
    文件路径时两者相等）。lineterm="" 让每行不带多余换行，由调用方决定拼接方式。
    """
    # 空文本按无行处理：旧文件不存在 = 新文件全是新增行
    old_lines = old_text.splitlines() if old_text else []
    new_lines = new_text.splitlines() if new_text else []
    # unified_diff 返回生成器，join 成整体文本返回
    return "\n".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=fromfile, tofile=tofile, lineterm=""))


def truncate_diff(diff: str, max_lines: int = 200) -> str:
    """超大 diff 显示截断：只留前 max_lines 行，末尾追加「… +N lines」省略标记。

    未超限时原样返回；防止确认预览刷屏（TTY 与非 TTY 共用同一截断）。
    """
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return diff
    # 截断：保留前 max_lines 行 + 一行省略标记，行数比原 diff 少
    return "\n".join(lines[:max_lines] + [f"… +{len(lines) - max_lines} lines"])
