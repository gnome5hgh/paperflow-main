"""MemoryApplyPatchTool：就地应用简化 unified diff（仅单块模式）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.core.memory.tools.runtime_context import get_memory_context


def _apply_diff(value: str, patch: str) -> str:
    """就地应用 unified diff：返回改动后的整块文本，语义不符时抛 ValueError。

    patch 行逐行处理：@@ 头跳到 hunk 起始（中间未改动行原样复制）；' ' 上下文
    行必须匹配目标当前行；'-' 删行从当前位置向后找首个匹配；'+' 增行插入到当前
    删除点。

    删行用前向扫描宽容匹配（'-' 行从当前位置向后找首个匹配并复制中间行），而
    ' ' 上下文行仍精确位置匹配——LLM 生成的 patch 与实际内容常有漂移，宽容删行
    吸收漂移、精确上下文守住边界。
    """
    import re
    lines = value.splitlines()
    result: list[str] = []
    i = 0
    for pline in patch.splitlines():
        if pline.startswith("@@"):
            m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", pline)
            if m:
                target = max(0, int(m.group(1)) - 1)
                while i < target and i < len(lines):
                    result.append(lines[i])
                    i += 1
            continue
        if pline.startswith(" "):
            c = pline[1:]
            if i >= len(lines) or lines[i] != c:
                raise ValueError(f"context line {c!r} not found at position {i + 1}")
            result.append(lines[i])
            i += 1
        elif pline.startswith("-"):
            removed = pline[1:]
            j = i
            while j < len(lines) and lines[j] != removed:
                j += 1
            if j >= len(lines):
                raise ValueError(f"line {removed!r} not found")
            while i < j:
                result.append(lines[i])
                i += 1
            i += 1
        elif pline.startswith("+"):
            result.append(pline[1:])
        elif pline.startswith("\\"):
            continue
    result.extend(lines[i:])
    return "\n".join(result)


def _memory_apply_patch(ctx, label: str, patch: str) -> str:
    """对指定块应用 patch；块缺失返回显式「no block」，多块 patch 直接拒绝。"""
    bm = ctx.block_manager
    block = bm.get_block_by_label(label)
    if block is None:
        return f"Error: no block with label {label}"
    if "*** Add Block:" in patch or "*** Update Block:" in patch:
        return "Error: multi-block patch not supported"
    try:
        result = _apply_diff(block.value, patch)
    except ValueError as e:
        return f"Error: {e}"
    try:
        bm.update_block_value(label, result)
    except ValueError as e:
        return f"Error: {e}"
    return f"Applied patch to block {label}"


class MemoryApplyPatchTool(Tool):
    name = "memory_apply_patch"
    description = "用简化 unified diff 更新记忆块"
    parameters = {
        "type": "object",
        "properties": {
            "label": {"type": "string"},
            "patch": {"type": "string", "description": "简化 unified diff"},
        },
        "required": ["label", "patch"],
    }
    risk_level = "medium"

    def execute(self, label: str, patch: str) -> ToolResult:
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            return ToolResult(text=_memory_apply_patch(ctx, label, patch))
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
