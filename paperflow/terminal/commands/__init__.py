"""REPL 斜杠命令：注册表基础设施 + 内置命令。

`registry.py` 是命令表与分发（契约），`builtin.py` 是内置命令实现。本包只做
再导出——主循环与测试都从 `paperflow.terminal.commands` 取。
"""

from .builtin import build_default_registry
from .registry import (CommandContext, CommandRegistry, DispatchResult,
                       SlashCommand)

__all__ = ["CommandContext", "CommandRegistry", "DispatchResult",
           "SlashCommand", "build_default_registry"]
