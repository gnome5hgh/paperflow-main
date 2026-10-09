"""memory-agent 的工具装配：记忆的全部读写（11 件，即 get_memory_tools() 全集）。

记忆是一类产物（核心块 + 清单 + 历史），按「一个 worker = 一类产物的责任人」，
它的读写只装配给本角色——其他角色一件不装，想记录只能派发本角色来记，所以
不存在「忘了记账」的静默失败（工具不在别人手里，想漏也漏不掉）。

不给寻址类工具（glob/grep）：路径与标题由调用方给出或本角色用 extract_title 核实；
工具是无状态类、执行时才取运行时上下文，故直接实例化列表（对齐 supervisor 的装配惯例）。
"""
from paperflow.tools.memory import get_memory_tools

#: 记忆读写全集（blocks 编辑 6 + 对话检索 1 + 清单与历史 4）
TOOLS = get_memory_tools()
