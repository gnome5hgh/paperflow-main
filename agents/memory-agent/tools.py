"""memory-agent 的工具装配：记忆读写全集（9 件）+ 读块内容的只读工具。

记忆是一类产物（核心块 + 未读清单），按「一个 worker = 一类产物的责任人」，
它的读写只装配给本角色——其他角色一件不装，想记录只能派发本角色来记，所以
不存在「忘了记账」的静默失败（工具不在别人手里，想漏也漏不掉）。

**为什么要 read_file / glob**：记忆工具里没有「读块」的动作——`memory` 只有
create/replace/delete/rename，而 `memory_replace` 要求给 `old_string`、
`memory_apply_patch` 要求对着当前内容写 diff，两者都以「已经看到当前内容」为前提。
MemFS 把每个块投影成 markdown（`<memory 根>/<块名>.md`，system 块在 `system/` 下），
所以读投影文件就是读块内容：这是本角色唯一的读通道，没有它只能盲写（末尾追加或整块
覆写），「把过时的那句改掉」这类定向增改就变成破坏性的。glob 用来枚举现有块文件名。

不给写文件工具：块的写入一律经记忆工具（走 BlockManager 的原子读-改-写 + CAS），
直接改投影文件会绕开那条路径。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools import GlobTool, ReadFileTool
from paperflow.tools.common.factory import make_tools
from paperflow.tools.memory import get_memory_tools

#: 记忆读写全集（blocks 编辑 6 + 对话检索 1 + 未读清单 2）+ 读块内容的只读工具。
# 记忆工具是无状态类、执行时才取运行时上下文，直接实例化；读工具经 make_tools 注入 _config。
TOOLS = get_memory_tools() + make_tools(PaperFlowConfig.from_env(), [ReadFileTool, GlobTool])
