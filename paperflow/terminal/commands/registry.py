# paperflow/terminal/commands/registry.py
"""斜杠命令的登记与分发——命令表基础设施，不含具体命令。

分发语义：首 token 等价匹配（非前缀、非包含）；无参命令带额外 token 时不
命中、静默当普通任务走（保留原 /exit「/exit now 命中不了」的行为）；未注册
的斜杠命令打 dim 提示后返回未消费，由主循环照旧送进意图管线。

handler 一律在 worker 线程执行（_repl 经 asyncio.to_thread 调 dispatch）：
skill 的交互确认走 prompt_toolkit，绝不能跑在事件循环线程上——与 io.read
的 to_thread 同因。handler 异常就地翻译打印，不杀 REPL。

内置命令见 builtin.py。
"""
import shlex
from dataclasses import dataclass
from typing import Callable

from ..common import translate_error
from ..io import InputIO
from ..render import StreamRenderer


@dataclass
class CommandContext:
    """斜杠命令的依赖注入袋：handler 只从这里拿依赖，不闭包抓主循环变量。

    Attributes:
        io: InputIO，输入适配（需要交互的子命令用）
        renderer: StreamRenderer，输出通道
        mcp_manager: MCP 管理器 | None（None 表示未接入）
    """

    io: InputIO
    renderer: StreamRenderer
    mcp_manager: object | None = None


@dataclass(frozen=True)
class DispatchResult:
    """一次分发的结局：consumed=已命中命令（主循环据此不进意图管线）；
    exit=命中了退出命令（handler 无法 break 主循环，用标志表达）。

    Attributes:
        consumed: bool，已命中命令（主循环据此不进意图管线）
        exit: bool，命中了退出命令（handler 无法 break 主循环，用标志表达）
    """

    consumed: bool = False
    exit: bool = False


@dataclass(frozen=True)
class SlashCommand:
    """一条斜杠命令。name 不含斜杠；exits=命中即退出 REPL；
    takes_args=False 时带额外 token 视为不命中（宁当任务）。

    Attributes:
        name: str，命令名（不含斜杠）
        usage: str，用法串（/help 展示）
        help: str，一句话说明
        handler: 回调，签名 (args, ctx) -> None
        exits: bool，命中即退出 REPL
        takes_args: bool，False 时带额外 token 视为不命中
    """

    name: str
    usage: str
    help: str
    handler: Callable[[list[str], CommandContext], None]
    exits: bool = False
    takes_args: bool = False


class CommandRegistry:
    """斜杠命令注册表。注册顺序即 /help 的列出顺序。

    Attributes:
        _context: CommandContext，handler 共用的依赖袋
        _commands: dict[str, SlashCommand]，命令名 → 命令
    """

    def __init__(self, context: CommandContext):
        """记录依赖袋并建空命令表（命令由构建函数后续注册）。

        Args:
            context: CommandContext，handler 共用的依赖袋
        """
        self._context = context
        self._commands: dict[str, SlashCommand] = {}

    def register(self, command: SlashCommand) -> None:
        """注册一条命令（同名覆盖）。

        Args:
            command: SlashCommand，待注册的命令
        """
        self._commands[command.name] = command

    def all_commands(self) -> list[SlashCommand]:
        """按注册顺序返回全部命令。

        Returns:
            SlashCommand 列表（/help 与测试遍历用）。
        """
        return list(self._commands.values())

    def dispatch(self, raw: str) -> DispatchResult:
        """解析并分发一条斜杠命令；未命中/消费与否用 DispatchResult 表达。

        Args:
            raw: str，用户输入的原始行（含前导斜杠）

        Returns:
            DispatchResult；handler 异常就地翻译打印，不杀 REPL。
        """
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


