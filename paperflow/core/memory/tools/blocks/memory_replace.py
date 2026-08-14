"""MemoryReplaceTool：替换记忆块中的精确子串（old_string 必须唯一）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context


def _memory_replace(ctx, label: str, old_string: str, new_string: str) -> str:
    """在块内替换子串：old_string 必须唯一出现才替换，0 次或 >1 次都报错。

    强制唯一是因为不明确的子串替换会静默改错位置或重复改；显式要求唯一逼
    LLM 补足上下文。块缺失返回显式「no block」——改既有块的意图不该被静默
    创建掩盖拼错（与 append 的自动建块刻意不对称）。
    """
    bm = ctx.block_manager
    block = bm.get_block_by_label(label)
    if block is None:
        return f"Error: no block with label {label}"
    value = block.value
    occurrences = value.count(old_string)
    if occurrences == 0:
        return f"Error: old_string not found in block {label}"
    if occurrences > 1:
        return f"Error: old_string occurs {occurrences} times, must be unique"
    try:
        bm.update_block_value(label, value.replace(old_string, new_string))
    except ValueError as e:
        return f"Error: {e}"
    return f"Updated block {label}: replaced '{old_string}' with '{new_string}'"


class MemoryReplaceTool(Tool):
    name = "memory_replace"
    description = "替换记忆块中的精确子串（old_string 必须唯一）"
    parameters = {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "记忆块标签"},
            "old_string": {"type": "string", "description": "要替换的旧子串（必须唯一）"},
            "new_string": {"type": "string", "description": "新子串"},
        },
        "required": ["label", "old_string", "new_string"],
    }
    risk_level = "medium"

    def execute(self, label: str, old_string: str, new_string: str) -> ToolResult:
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            return ToolResult(text=_memory_replace(ctx, label, old_string, new_string))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
