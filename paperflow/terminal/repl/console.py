# paperflow/terminal/repl/console.py
"""开场与逐行输出的呈现：打印函数工厂、路径缩写、启动横幅。

`_make_print_fn` 按终端类型给出打印函数（TTY 走 rich console.print 以支持样式
与折行，非 TTY 走内置 print 且忽略样式——不产生 ANSI 转义）；横幅与路径缩写在
REPL 开场打一次。三者都只产出文本或写出一行，不持有任何状态。
"""
from pathlib import Path


def _make_print_fn(console):
    """
    根据终端类型构造打印函数。

    Args:
        console: rich.Console 实例，若为 None（非 TTY）则使用内置 print。

    Returns:
        打印函数，接受 *args, style=None, end="\n", flush=False 等参数。
            若 console 非 None，使用 console.print 并支持 style（如 dim 样式）；
            若 console 为 None，使用内置 print 并忽略 style（不产生 ANSI 转义）。
    """
    if console is None:
        def _plain(*args, style=None, **kwargs):
            """非 TTY 打印：走内置 print（忽略 style，不产生 ANSI 转义）。

            Args:
                args: 任意位置参数，原样转给 print
                style: str | None，rich 样式名（此处忽略）
                kwargs: dict，其余 print 关键字参数（end/flush 等）
            """
            print(*args, **kwargs)
        return _plain

    def _rich(*args, style=None, end="\n", flush=False):
        """TTY 打印：走 rich console.print，支持样式与长行折行。

        Args:
            args: 任意位置参数，原样转给 console.print
            style: str | None，rich 样式名
            end: str，行尾字符
            flush: bool，是否立即刷新（rich 由 console 管理）
        """
        # overflow="fold"：长行自动换行而非截断
        console.print(*args, style=style, end=end, overflow="fold")
    return _rich


def _shorten_path(p: str) -> str:
    """将路径中的 home 目录缩写为 '~'，用于 banner 显示。

    Args:
        p: str，绝对路径

    Returns:
        home 目录前缀替换为 "~" 的路径（用于横幅显示）。
    """
    home = str(Path.home())
    return "~" + p[len(home):] if str(p).startswith(home) else str(p)


def _render_banner(model: str, workspace: str) -> str:
    """
    生成启动横幅，使用 box-drawing 字符绘制方框。

    Args:
        model: 模型名称（如 "gpt-4"）。
        workspace: 工作区路径（已缩写）。

    Returns:
        str: 多行横幅字符串，包含标题、模型和工作区信息。
    """
    lines = [
        ">_ paperFlow Academic Assistant",
        "",
        f"model:     {model}",
        f"workspace: {workspace}",
    ]
    inner = max(len(l) for l in lines)
    top = "╭" + "─" * (inner + 2) + "╮"
    body = "\n".join(f"│ {l:<{inner}} │" for l in lines)
    bottom = "╰" + "─" * (inner + 2) + "╯"
    return f"{top}\n{body}\n{bottom}"


