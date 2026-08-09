"""core memory 编辑工具函数（命名对齐 Letta function_sets/base.py）。

各函数以 ctx（含 block_manager / agent_id）为第一参数，ToolManager 用
_FunctionTool 包装后注入 agent 工具面。
"""
from __future__ import annotations

from paperflow.core.memory.services.block_manager import BlockManager

__all__ = ["memory_replace", "memory_insert", "memory_rethink",
           "memory_finish_edits", "memory", "memory_apply_patch"]


def memory_replace(ctx, label: str, old_string: str, new_string: str) -> str:
    """精确子串替换；old_string 必须逐字唯一，多匹配报错（Letta 同款语义）。"""
    bm: BlockManager = ctx.block_manager
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


def memory_insert(ctx, label: str, new_string: str, insert_line: int = -1) -> str:
    """指定行号后插入；-1=末尾，0=开头。"""
    bm: BlockManager = ctx.block_manager
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


def memory_rethink(ctx, label: str, new_memory: str) -> str:
    """整块重写。"""
    bm: BlockManager = ctx.block_manager
    try:
        bm.update_block_value(label, new_memory)
    except ValueError as e:
        return f"Error: {e}"
    return f"Rewrote block {label}"


def memory_finish_edits(ctx) -> str:
    """结束本次记忆编辑（Letta 结束信号）。"""
    return "Memory edits complete."


def memory(ctx, action: str, label: str, value: str | None = None, **kwargs) -> str:
    """统一块管理：create / replace / delete / rename。

    label 在 blocks 表无 UNIQUE 约束——create/rename 必须先查重，
    否则重复 label 会静默建出不可达的幽灵块。
    """
    bm: BlockManager = ctx.block_manager
    if action == "create":
        if bm.get_block_by_label(label) is not None:
            return f"Error: label '{label}' already exists"
        bm.create_block(label, value or "")
        return f"Created block {label}"
    if action == "replace":
        return memory_rethink(ctx, label, value or "")
    if action == "delete":
        b = bm.get_block_by_label(label)
        if b is None:
            return f"Error: no block with label {label}"
        if b.read_only:
            return "Error: block is read-only"
        bm.delete_block(b.id)
        return f"Deleted block {label}"
    if action == "rename":
        b = bm.get_block_by_label(label)
        if b is None:
            return f"Error: no block with label {label}"
        # label 无独立 update 接口——重建并保留元数据（read_only/description/limit）
        new_label = kwargs.get("new_label", value or label)
        if new_label == label:
            return f"Renamed block {label} -> {new_label}"
        if bm.get_block_by_label(new_label) is not None:
            return f"Error: label '{new_label}' already exists"
        bm.create_block(new_label, b.value, limit=b.limit,
                        description=b.description, read_only=b.read_only)
        # rename 是重建（保护元数据迁移到新块），不是删除——走不检查 read_only 的
        # 底层 _delete；直接 delete_block 会因 read_only 拒绝而留下半完成的孤儿新块。
        bm._delete(b.id)
        return f"Renamed block {label} -> {new_label}"
    return f"Error: unknown action {action}"


def memory_apply_patch(ctx, label: str, patch: str) -> str:
    """就地应用简化 unified diff（Letta 同款）。仅支持单块模式：@@ 行号上下文 +
    -/+ 行；多块模式（*** Add Block: 等）返回明确错误（不实现）。

    语义：逐 hunk 删 - 行、在对应位置插 + 行（不是「删全部再追加末尾」）。
    - 行从当前位置向后找首个匹配删除；+ 行插入到删除点；' ' 前缀行是上下文锚点；
    @@ 头部把游标跳到 hunk 起始（中间未改动行原样保留）。
    """
    import re
    bm: BlockManager = ctx.block_manager
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


def _apply_diff(value: str, patch: str) -> str:
    """就地应用 unified diff：返回改动后的整块文本，语义不符时抛 ValueError。

    patch 行的逐行处理：@@ 头跳到 hunk 起始（中间未改动行原样复制）；' ' 上下文
    行必须匹配目标当前行；'-' 删行从当前位置向后找首个匹配（无上下文锚定时的
    简化 diff）；'+' 增行插入到当前删除点。
    """
    import re
    lines = value.splitlines()
    result: list[str] = []
    i = 0                      # 目标行游标（指向原始 lines）
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
            ctx = pline[1:]
            if i >= len(lines) or lines[i] != ctx:
                raise ValueError(f"context line {ctx!r} not found at position {i + 1}")
            result.append(lines[i])
            i += 1
        elif pline.startswith("-"):
            removed = pline[1:]
            j = i
            while j < len(lines) and lines[j] != removed:
                j += 1
            if j >= len(lines):
                raise ValueError(f"line {removed!r} not found")
            while i < j:                      # 删除点之前未匹配的行按上下文保留
                result.append(lines[i])
                i += 1
            i += 1                            # 跳过被删行
        elif pline.startswith("+"):
            result.append(pline[1:])
        elif pline.startswith("\\"):
            continue                          # "\ No newline at end of file"
    result.extend(lines[i:])
    return "\n".join(result)
