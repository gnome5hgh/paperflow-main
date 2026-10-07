"""MemoryInsertTool：在记忆块指定行号后插入内容（-1=末尾，0=开头）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.memory.runtime_context import get_memory_context


def _memory_insert(ctx, label: str, new_string: str, insert_line: int = -1) -> str:
    """把 new_string 插入块的指定行号；insert_line=-1 表示追加到末尾。

    块缺失返回显式「no block」——改既有块的意图不该被静默创建掩盖拼错。
    行号超出末尾时按末尾处理（splitlines 后 insert 会就地落在末尾）。读-算-写收进
    mutate_block 的 mutator，整段一次持锁。
    """
    bm = ctx.block_manager
    # 回报实际插入行号：默认 -1 落末尾时报告末尾位置，不暴露 -1。mutator 每次调用都
    # 重算，最终保留的是真正写入所用的那一行。
    resolved_line = insert_line

    def _insert(v: str) -> str:
        """在 v 的 insert_line 处插入一行；-1 表示末尾，超出末尾按末尾处理。"""
        nonlocal resolved_line
        lines = v.splitlines()
        at = len(lines) if insert_line == -1 else insert_line
        resolved_line = at
        lines.insert(at, new_string)
        return "\n".join(lines)

    try:
        bm.mutate_block(label, _insert)
    except KeyError:
        return f"Error: no block with label {label}"
    except ValueError as e:
        return f"Error: {e}"
    return f"Inserted into block {label} at line {resolved_line}"


class MemoryInsertTool(Tool):
    name = "memory_insert"
    description = "在记忆块指定行插入内容"
    parameters = {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "new_string": {"type": "string"},
            "insert_line": {"type": "integer", "description": "插入行号；-1=末尾，0=开头"},
        },
        "required": ["label", "new_string"],
    }
    risk_level = "medium"

    def execute(self, label: str, new_string: str, insert_line: int = -1) -> ToolResult:
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            return ToolResult(text=_memory_insert(ctx, label, new_string, insert_line))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
