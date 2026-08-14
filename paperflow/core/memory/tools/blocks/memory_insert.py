"""MemoryInsertTool：在记忆块指定行号后插入内容（-1=末尾，0=开头）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context


def _memory_insert(ctx, label: str, new_string: str, insert_line: int = -1) -> str:
    """把 new_string 插入块的指定行号；insert_line=-1 表示追加到末尾。

    块缺失返回显式「no block」——改既有块的意图不该被静默创建掩盖拼错。
    行号超出末尾时按末尾处理（splitlines 后 insert 会就地落在末尾）。
    """
    bm = ctx.block_manager
    block = bm.get_block_by_label(label)
    if block is None:
        return f"Error: no block with label {label}"
    lines = block.value.splitlines()
    if insert_line == -1:
        insert_line = len(lines)
    lines.insert(insert_line, new_string)
    try:
        bm.update_block_value(label, "\n".join(lines))
    except ValueError as e:
        return f"Error: {e}"
    return f"Inserted into block {label} at line {insert_line}"


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
