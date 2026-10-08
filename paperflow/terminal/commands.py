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


def _exit_handler(args: list[str], ctx: CommandContext) -> None:
    """退出命令的占位 handler（退出由 SlashCommand.exits 标志表达）。

    Args:
        args: list[str]，命令参数（未使用）
        ctx: CommandContext，依赖袋（未使用）
    """
    pass  # 退出由 SlashCommand.exits 标志表达，handler 无事可做


def _mcp_handler(args: list[str], ctx: CommandContext) -> None:
    """/mcp：打印各 server 状态报告。

    Args:
        args: list[str]，命令参数（未使用）
        ctx: CommandContext，依赖袋
    """
    ctx.renderer.print(ctx.mcp_manager.status_report()
                       if ctx.mcp_manager
                       else "未接入 MCP（config.yaml 顶层 mcp_servers 为空）。")


def _version_handler(args: list[str], ctx: CommandContext) -> None:
    """/version：打印已安装的 paperflow 版本。

    Args:
        args: list[str]，命令参数（未使用）
        ctx: CommandContext，依赖袋
    """
    from importlib.metadata import PackageNotFoundError, version
    try:
        v = version("paperflow")
    except PackageNotFoundError:
        v = "unknown（开发环境：见 pyproject.toml）"
    ctx.renderer.print(f"paperflow {v}")


def _help_handler(args: list[str], ctx: CommandContext,
                  registry: CommandRegistry) -> None:
    """/help：按注册顺序列出可用命令。

    Args:
        args: list[str]，命令参数（未使用）
        ctx: CommandContext，依赖袋
        registry: CommandRegistry，命令来源
    """
    lines = ["可用命令："]
    for cmd in registry.all_commands():
        lines.append(f"  {cmd.usage}  {cmd.help}")
    ctx.renderer.print("\n".join(lines))


_SKILL_USAGE = """用法：
  /skill install <source> [--ref REF] [-y] [--allow-code]
  /skill list
  /skill uninstall <name>
  /skill update <name> [--allow-code]
  /skill enable <name>
  /skill disable <name>"""


def _build_skill_parser() -> argparse.ArgumentParser:
    """skill 子命令解析。参数语义与原 paperflow skill CLI（cli.py 迁出）逐字一致。
    用法错误的抛法分两路：required 校验（缺子命令/缺必选参数）经 parser.error()
    抛 SystemExit(2)，不受 exit_on_error=False 影响；invalid choice 等场景抛
    ArgumentError。handler 两者都接，只打用法、不杀 REPL。"""
    parser = argparse.ArgumentParser(prog="/skill", add_help=False,
                                     exit_on_error=False)
    action = parser.add_subparsers(dest="skill_action", required=True)
    inst = action.add_parser("install", add_help=False, exit_on_error=False)
    inst.add_argument("source")
    inst.add_argument("--ref", default=None, metavar="REF",
                      help="git 来源的分支/标签（缺省默认 HEAD；lock 记录之，update 按其重装）")
    inst.add_argument("-y", "--yes", action="store_true", dest="yes",
                      help="跳过确认（仅纯指令 skill）")
    inst.add_argument("--allow-code", action="store_true",
                      help="允许捆绑 tools.py 的 skill（安装前必须人工审读代码）")
    action.add_parser("list", add_help=False, exit_on_error=False)
    uni = action.add_parser("uninstall", add_help=False, exit_on_error=False)
    uni.add_argument("name")
    upd = action.add_parser("update", add_help=False, exit_on_error=False)
    upd.add_argument("name")
    upd.add_argument("--allow-code", action="store_true",
                     help="新版本捆绑 tools.py 时显式放行（旧版本装过不豁免）")
    for name in ("enable", "disable"):
        sub = action.add_parser(name, add_help=False, exit_on_error=False)
        sub.add_argument("name")
    return parser


def _skill_handler(argv: list[str], ctx: CommandContext) -> None:
    """/skill：install/list/uninstall/update/enable/disable 子命令分发。

    Args:
        argv: list[str]，子命令与参数
        ctx: CommandContext，依赖袋
    """
    # 惰性导入：commands.py 被每次 _repl 装配拉起，不常驻拖 core.skills 依赖链
    from paperflow.core.skills import (
        enable_skill, install_skill, list_skills_command,
        uninstall_skill, update_skill)
    try:
        args = _build_skill_parser().parse_args(argv)
    except (argparse.ArgumentError, SystemExit):
        ctx.renderer.print(_SKILL_USAGE)
        return
    # 与原 CLI 同一锚定基准：skill 根目录 = <cwd>/.paperflow/
    pf_dir = Path.cwd() / ".paperflow"
    skills_dir = pf_dir / "skills"
    # print_fn 必须 print_raw：skill 输出是任意文本（名称/描述/license 可含
    # [x][/x] 片段），renderer.print 会被 rich 当标记解析抛 MarkupError。
    try:
        if args.skill_action == "install":
            install_skill(args.source, pf_dir, ref=args.ref,
                          assume_yes=args.yes, allow_code=args.allow_code,
                          confirm=ctx.io.confirm,
                          print_fn=ctx.renderer.print_raw)
        elif args.skill_action == "list":
            list_skills_command(str(skills_dir) if skills_dir.is_dir() else None,
                                pf_dir, print_fn=ctx.renderer.print_raw)
        elif args.skill_action == "uninstall":
            uninstall_skill(args.name, pf_dir,
                            print_fn=ctx.renderer.print_raw)
        elif args.skill_action == "update":
            update_skill(args.name, pf_dir, allow_code=args.allow_code,
                         print_fn=ctx.renderer.print_raw)
        elif args.skill_action == "enable":
            enable_skill(args.name, pf_dir, enabled=True,
                         print_fn=ctx.renderer.print_raw)
        else:  # disable
            enable_skill(args.name, pf_dir, enabled=False,
                         print_fn=ctx.renderer.print_raw)
    except ValueError as e:
        # lock schema 版本不符等治理错误：友好提示，不裸 traceback
        ctx.renderer.print(f"错误：{e}", style="red")


def build_default_registry(context: CommandContext) -> CommandRegistry:
    """内置命令注册表；/help 收尾注册以闭包持有 registry 自身。

    Args:
        context: CommandContext，handler 共用的依赖袋

    Returns:
        注册好全部内置命令的 CommandRegistry。
    """
    registry = CommandRegistry(context)
    registry.register(SlashCommand("exit", "/exit", "退出 REPL",
                                   _exit_handler, exits=True))
    registry.register(SlashCommand("mcp", "/mcp", "MCP server 状态一览",
                                   _mcp_handler))
    registry.register(SlashCommand("version", "/version", "显示版本号",
                                   _version_handler))
    registry.register(SlashCommand(
        "skill", "/skill install|list|uninstall|update|enable|disable",
        "skill 安装管理", _skill_handler, takes_args=True))
    registry.register(SlashCommand(
        "help", "/help", "列出全部命令",
        lambda args, ctx: _help_handler(args, ctx, registry)))
    return registry
