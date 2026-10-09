"""REPL 输入适配：契约 + 两种终端实现 + 按终端类型装配的工厂。

`base.py` 定义契约与并发确认锁，`interactive.py`（TTY）与 `fallback.py`（非 TTY）
是它的两个可互换实现。本包只做再导出与装配——消费方（cli / repl / 测试）从
`paperflow.terminal.io` 取，不直接引内部模块路径。
"""
import sys
from pathlib import Path

from .base import InputIO
from .fallback import FallbackIO
from .interactive import PromptToolkitIO


def make_input_io(config) -> InputIO:
    """
    工厂函数，根据终端类型装配合适的输入适配器。

    Args:
        config: 配置对象，需要包含 runtime.workspace 属性（字符串），用于存放历史文件。

    Returns:
        InputIO: 若标准输入为 TTY，则返回 PromptToolkitIO 实例，历史文件保存在 workspace 下；
                 否则返回 FallbackIO 实例（用于管道/CI/测试）。

    Note:
        TTY 环境下历史文件固定命名为 repl_history.txt，存放在 workspace 的 session/ 目录中。
    """
    if sys.stdin.isatty():
        # 主输入历史文件放在 workspace 下，跨会话保留
        return PromptToolkitIO(str(Path(config.runtime.workspace) / "session" / "repl_history.txt"))
    return FallbackIO()


__all__ = ["InputIO", "FallbackIO", "PromptToolkitIO", "make_input_io"]
