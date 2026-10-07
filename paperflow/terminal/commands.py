# paperflow/terminal/commands.py
"""REPL 斜杠命令：注册表 + 内置命令实现。

分发语义：首 token 等价匹配（非前缀、非包含）；无参命令带额外 token 时不
命中、静默当普通任务走（保留原 /exit「/exit now 命中不了」的行为）；未注册
的斜杠命令打 dim 提示后返回未消费，由主循环照旧送进意图管线。

handler 一律在 worker 线程执行（_repl 经 asyncio.to_thread 调 dispatch）：
skill 的交互确认走 prompt_toolkit，绝不能跑在事件循环线程上——与 io.read
的 to_thread 同因。handler 异常就地翻译打印，不杀 REPL。
"""
import argparse
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from paperflow.terminal.errors import translate_error
from paperflow.terminal.io import InputIO
from paperflow.terminal.render import StreamRenderer


@dataclass
class CommandContext:
    """斜杠命令的依赖注入袋：handler 只从这里拿依赖，不闭包抓主循环变量。"""

    io: InputIO
    renderer: StreamRenderer
    mcp_manager: object | None = None


@dataclass(frozen=True)
class DispatchResult:
    """一次分发的结局：consumed=已命中命令（主循环据此不进意图管线）；
    exit=命中了退出命令（handler 无法 break 主循环，用标志表达）。"""

    consumed: bool = False
    exit: bool = False


@dataclass(frozen=True)
class SlashCommand:
    """一条斜杠命令。name 不含斜杠；exits=命中即退出 REPL；
    takes_args=False 时带额外 token 视为不命中（宁当任务）。"""

    name: str
    usage: str
    help: str
    handler: Callable[[list[str], CommandContext], None]
    exits: bool = False
    takes_args: bool = False


class CommandRegistry:
    """斜杠命令注册表。注册顺序即 /help 的列出顺序。"""

    def __init__(self, context: CommandContext):
        self._context = context
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        self._commands[command.name] = command

    def all_commands(self) -> list[SlashCommand]:
        return list(self._commands.values())

    def dispatch(self, raw: str) -> DispatchResult:
        try:
            tokens = shlex.split(raw)
        except ValueError:
            tokens = raw.split()
        if not tokens:
            return DispatchResult()
        first, rest = tokens[0], tokens[1:]
        # 键存的是不含斜杠的 name，首 token 需剥掉斜杠后再等价匹配
        command = self._commands.get(first[1:]) if first.startswith("/") else None
        if command is None:
            if first.startswith("/"):
                self._context.renderer.print(
                    f"未知斜杠命令：{first}（/help 查看可用命令）", style="dim")
            return DispatchResult()
        if rest and not command.takes_args:
            return DispatchResult()
        try:
            command.handler(rest, self._context)
        except Exception as e:  # 单条命令失败不带走 REPL
            self._context.renderer.print(translate_error(e), style="red")
        return DispatchResult(consumed=True, exit=command.exits)


def _exit_handler(args: list[str], ctx: CommandContext) -> None:
    pass  # 退出由 SlashCommand.exits 标志表达，handler 无事可做


def _mcp_handler(args: list[str], ctx: CommandContext) -> None:
    ctx.renderer.print(ctx.mcp_manager.status_report()
                       if ctx.mcp_manager
                       else "未接入 MCP（config.yaml 顶层 mcp_servers 为空）。")


def _version_handler(args: list[str], ctx: CommandContext) -> None:
    from importlib.metadata import PackageNotFoundError, version
    try:
        v = version("paperflow")
    except PackageNotFoundError:
        v = "unknown（开发环境：见 pyproject.toml）"
    ctx.renderer.print(f"paperflow {v}")


def _help_handler(args: list[str], ctx: CommandContext,
                  registry: CommandRegistry) -> None:
    lines = ["可用命令："]
    for cmd in registry.all_commands():
        lines.append(f"  {cmd.usage}  {cmd.help}")
    ctx.renderer.print("\n".join(lines))


def build_default_registry(context: CommandContext) -> CommandRegistry:
    """内置命令注册表；/help 收尾注册以闭包持有 registry 自身。"""
    registry = CommandRegistry(context)
    registry.register(SlashCommand("exit", "/exit", "退出 REPL",
                                   _exit_handler, exits=True))
    registry.register(SlashCommand("mcp", "/mcp", "MCP server 状态一览",
                                   _mcp_handler))
    registry.register(SlashCommand("version", "/version", "显示版本号",
                                   _version_handler))
    registry.register(SlashCommand(
        "help", "/help", "列出全部命令",
        lambda args, ctx: _help_handler(args, ctx, registry)))
    return registry
