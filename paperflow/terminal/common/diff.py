# paperflow/terminal/diff.py
"""统一 diff 计算与显示截断。

写/编辑工具的确认预览与展示共用：difflib.unified_diff 算差异、按行数截断超大
diff，避免终端被刷屏。纯函数、无 IO，测试可直驱。
"""
import difflib


def compute_diff(old_text: str, new_text: str, fromfile: str = "old", tofile: str = "new") -> str:
    """
    生成 unified diff 文本，用于在确认写/编辑操作前向用户展示将要发生的变化。

    Args:
        old_text (str): 旧文件内容（文件修改前）。如果为空字符串，表示文件不存在或新文件。
        new_text (str): 新文件内容（修改后）。如果为空字符串，表示文件将被删除或清空。
        fromfile (str): diff 头部中旧文件的标签，通常为文件路径，默认 "old"。
        tofile (str): diff 头部中新文件的标签，通常与 fromfile 相同，默认 "new"。

    Returns:
        str: 多行 unified diff 字符串，包含上下文（-3/+3 行）。如果 old_text 和 new_text
             完全相同，返回空字符串（difflib.unified_diff 不产生任何差异行）。

    Notes:
        - 空文本按空行列表处理，即文件不存在时视作新增所有行，删除时视作删除所有行。
        - lineterm="" 确保每行不带多余换行符，由调用方决定最终拼接方式（通常用 "\n".join）。
        - difflib.unified_diff 返回生成器，本函数通过 "\n".join() 将其转换为单个字符串。
        - 差异行数较多时，建议配合 truncate_diff 截断，避免刷屏。
    """
    # 空文本按无行处理：旧文件不存在 = 新文件全是新增行
    old_lines = old_text.splitlines() if old_text else []
    new_lines = new_text.splitlines() if new_text else []
    # unified_diff 返回生成器，join 成整体文本返回
    return "\n".join(difflib.unified_diff(
        old_lines, new_lines, fromfile=fromfile, tofile=tofile, lineterm=""))


def truncate_diff(diff: str, max_lines: int = 200) -> str:
    """
    截断超长 diff，防止终端被海量输出刷屏。

    Args:
        diff (str): 完整的 diff 文本（通常由 compute_diff 生成）。
        max_lines (int): 保留的最大行数（不包括省略标记行），默认 200。

    Returns:
        str: 如果 diff 行数 <= max_lines，原样返回；否则截断为前 max_lines 行，
             并在末尾追加一行省略标记，形如 "… +N lines"，N 为被截掉的行数。

    Notes:
        - 省略标记计入总行数，但不算在 max_lines 内（原 diff 行数 > max_lines 时，
          返回的行数为 max_lines + 1）。
        - 截断仅按行数，不考虑 diff 语义完整性，目的是终端可读性。
        - 该函数仅做文本截断，不涉及终端颜色或样式。
    """
    lines = diff.splitlines()
    if len(lines) <= max_lines:
        return diff
    # 截断：保留前 max_lines 行 + 一行省略标记，行数比原 diff 少
    return "\n".join(lines[:max_lines] + [f"… +{len(lines) - max_lines} lines"])