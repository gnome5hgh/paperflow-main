# paperflow/core/security/services/command.py
"""
命令解析与风险判定：把一条 shell 命令拆成逐条命令与 argv，再逐条定档。

这是安全模块的**独立防护能力**（与出网出口同层同角色）：由中间件调用，不进洋葱
模型本身。

**为什么用语法树而不是正则**：命令拼在一起时（``a && b | c; d``）正则只能看到
一坨字符串；而判风险需要 argv——

    rm -rf /work/tmp     容器里清自己的临时目录，合法
    rm -rf /             毁灭性

两句话在**命令词层面完全一样**，只有拿到参数才分得开。tree-sitter 把命令拆成
逐条命令 + 各自 argv，熔断判据因此可以看参数而不只看命令名。

判定的三档结果（顺序即优先级）：

1. **熔断**（``circuit_break``）：无论参数如何都毁灭，或参数指向毁灭性目标——
   提权（``sudo``）与格式化（``mkfs``）没有合法形态；``rm -rf`` / ``dd`` 只在
   目标指向根、系统目录或设备节点时熔断。命中即硬拒。
2. **未知**（``parse_failed``）：解析不出结构（含语法错误节点）→ 不猜，升级确认。
3. **不在安全清单内**（``unsafe``）：命令名不在只读/可重入清单里（含执行器、
   联网、装包类）→ 升级确认。

**它是策略，不是隔离**——本项目没有隔离轴，所以这张表是命令与宿主之间唯一的东西，
不配当那堵墙。
"""

import os
import re
from dataclasses import dataclass

import tree_sitter_bash
from tree_sitter import Language, Parser

#: shell 语法与解析器（模块级建一次；解析器非线程安全，故每次调用新建）
_BASH_LANGUAGE = Language(tree_sitter_bash.language())


def _parser() -> Parser:
    """新建一个 bash 解析器（Parser 不是线程安全的，别共享实例）。"""
    return Parser(_BASH_LANGUAGE)


#: 只读/可重入命令清单：这些命令名单独出现时不打扰用户。
#: **刻意不含执行器与联网、装包类**（sh/bash/python/curl/wget/pip/apt…）——
#: 它们能把任意代码或数据带进来，一律升级确认。也刻意不含 rm/mv/dd/chmod 这类
#: 会改文件或权限的，它们要按参数判（见下面两条规则）。
SAFE_COMMANDS = frozenset({
    "ls", "cat", "head", "tail", "wc", "grep", "find", "echo", "printf",
    "sort", "uniq", "cut", "sed", "awk", "tr", "diff", "comm", "paste",
    "join", "nl", "tac", "basename", "dirname", "realpath", "readlink",
    "stat", "file", "tree", "du", "df", "pwd", "which", "true", "false",
    "test", "mkdir", "cp", "touch", "tar", "unzip", "gzip", "gunzip",
    "jq", "date", "seq", "sleep", "xargs", "sha256sum", "md5sum",
})

#: 无条件熔断的命令：提权与格式化没有「安全的参数组合」这回事
UNCONDITIONAL_BREAK = frozenset({"sudo", "doas", "mkfs", "mkfs.ext4", "mkfs.xfs",
                                 "mkswap", "fdisk", "parted"})

#: 文件系统根与系统目录：递归删除/写设备落在这里即熔断
ROOT_TARGETS = frozenset({"/", "/etc", "/usr", "/bin", "/sbin", "/var", "/lib",
                          "/boot", "/dev", "/proc", "/sys", "/System", "/Library",
                          "/private", "/Users", "/home", "~", "$HOME"})

#: fork 炸弹的经典形态——语法树把它拆成函数定义 + 递归管道，形态识别不如直接
#: 匹配这串特征（它就是一段固定写法）
_FORK_BOMB_RE = re.compile(r":\s*\(\s*\)\s*\{.*:\s*\|\s*:.*&.*\}\s*;?\s*:")

#: rm 的递归与强制标志（任意组合写法）
_RM_RECURSIVE_RE = re.compile(r"^-[a-zA-Z]*r", re.IGNORECASE)
_RM_FORCE_RE = re.compile(r"^-[a-zA-Z]*f", re.IGNORECASE)


@dataclass(frozen=True)
class Command:
    """从一条命令行里拆出来的一条命令。

    Attributes:
        name: str，命令名（如 "rm"、"curl"）
        args: tuple[str, ...]，该命令的参数（不含命令名）
        text: str，这条命令的原文片段
    """

    name: str
    args: tuple[str, ...] = ()
    text: str = ""


@dataclass(frozen=True)
class CommandAnalysis:
    """一次命令解析与定档的结果。

    Attributes:
        commands: tuple[Command, ...]，拆出来的全部命令（含管道两侧）
        circuit_break: tuple[str, ...]，触发熔断的理由（空 = 没触发）
        unsafe: tuple[str, ...]，不在安全清单内的命令名（需升级确认）
        parse_failed: bool，解析含语法错误 → 不猜，按未知处理（升级确认）
    """

    commands: tuple[Command, ...] = ()
    circuit_break: tuple[str, ...] = ()
    unsafe: tuple[str, ...] = ()
    parse_failed: bool = False

    @property
    def needs_escalation(self) -> bool:
        """是否需要升级人工确认（未知或有不在清单内的命令）。"""
        return self.parse_failed or bool(self.unsafe)


def _command_nodes(node):
    """递归收集全部 ``command`` 节点（管道与 &&/; 两侧各算一条）。"""
    found = []
    if node.type == "command":
        found.append(node)
    for child in node.children:
        found.extend(_command_nodes(child))
    return found


def parse(text: str) -> tuple[tuple[Command, ...], bool]:
    """把命令文本拆成逐条命令，返回 (命令列表, 是否解析失败)。

    Args:
        text: str，待解析的命令文本（可以是 ``a && b | c`` 这样的拼接）

    Returns:
        tuple：命令元组 + 解析是否含语法错误（含错误即未知，调用方据此处置）。
    """
    tree = _parser().parse(text.encode("utf-8"))
    failed = bool(tree.root_node.has_error)
    commands: list[Command] = []
    for node in _command_nodes(tree.root_node):
        name_node = node.child_by_field_name("name")
        name = (name_node.text or b"").decode("utf-8", "replace") if name_node else ""
        if not name:
            # 命令名取不到（语法错误节点等）——按未知处理
            failed = True
            continue
        args = []
        for child in node.children:
            if child.type == "command_name":
                continue          # 命令名不算参数
            args.append((child.text or b"").decode("utf-8", "replace"))
        commands.append(Command(
            name=name,
            args=tuple(args),
            text=(node.text or b"").decode("utf-8", "replace"),
        ))
    return tuple(commands), failed


def _is_root_target(target: str) -> bool:
    """目标是否指向文件系统根、系统目录或家目录（含其下任意深度）。

    ``rm -rf /work/tmp`` 不该熔断（它在受控目录里），``rm -rf /``、``rm -rf /etc``
    与 ``rm -rf /etc/nginx`` 都必须熔断。判据：目标消解 ``.``/``..`` 后落在
    ROOT_TARGETS 的任一项之下（按路径前缀，不是字符串前缀），或写法里带 ``..``
    （往上跳的意图明确，不猜）。
    """
    if not target or target.startswith("-"):
        return False
    if target in ("~", "$HOME") or target.startswith("~/"):
        return True
    if ".." in target.split("/"):
        return True
    normalized = os.path.normpath(target)
    for root in ROOT_TARGETS:
        if not root.startswith("/"):
            continue
        if normalized == root or normalized.startswith(root + "/"):
            return True
    return False


def _circuit_break_reasons(cmd: Command) -> list[str]:
    """单条命令是否触发熔断，返回理由列表（空 = 不熔断）。"""
    reasons: list[str] = []
    name = cmd.name

    if name in UNCONDITIONAL_BREAK:
        reasons.append(f"{name}：提权/格式化类指令")

    if name == "rm":
        recursive = any(_RM_RECURSIVE_RE.match(a) for a in cmd.args)
        force = any(_RM_FORCE_RE.match(a) for a in cmd.args)
        if recursive and force and any(_is_root_target(a) for a in cmd.args):
            reasons.append("rm -rf 的目标是根/系统目录")

    if name == "dd":
        for arg in cmd.args:
            if arg.startswith("of=") and arg[3:].startswith("/dev/"):
                reasons.append("dd 直接写设备节点")

    return reasons


def analyze(text: str) -> CommandAnalysis:
    """解析并定档一条命令文本。

    Args:
        text: str，命令文本

    Returns:
        CommandAnalysis：拆出的命令、熔断理由、不在清单内的命令名、是否解析失败。
    """
    commands, failed = parse(text)

    reasons: list[str] = []
    unsafe: list[str] = []
    for cmd in commands:
        reasons.extend(_circuit_break_reasons(cmd))
        if cmd.name not in SAFE_COMMANDS and cmd.name not in UNCONDITIONAL_BREAK:
            unsafe.append(cmd.name)

    # fork 炸弹的形态特征：语法树拆得出来但看不出「它是炸弹」，故对原文判一次
    if _FORK_BOMB_RE.search(text):
        reasons.append("fork 炸弹形态")

    return CommandAnalysis(
        commands=commands,
        circuit_break=tuple(dict.fromkeys(reasons)),
        unsafe=tuple(dict.fromkeys(unsafe)),
        parse_failed=failed,
    )
