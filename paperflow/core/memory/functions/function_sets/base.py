"""core memory 编辑工具函数（命名对齐 Letta function_sets/base.py）。

各函数以 ctx（含 block_manager / agent_id）为第一参数，ToolManager 用
_FunctionTool 包装后注入 agent 工具面。
"""
from __future__ import annotations

import re

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
    """统一块管理：create / replace / delete / rename。"""
    bm: BlockManager = ctx.block_manager
    if action == "create":
        bm.create_block(label, value or "")
        return f"Created block {label}"
    if action == "replace":
        return memory_rethink(ctx, label, value or "")
    if action == "delete":
        b = bm.get_block_by_label(label)
        if b is None:
            return f"Error: no block with label {label}"
        bm.delete_block(b.id)
        return f"Deleted block {label}"
    if action == "rename":
        b = bm.get_block_by_label(label)
        if b is None:
            return f"Error: no block with label {label}"
        # label 无独立 update 接口——重建
        new_label = kwargs.get("new_label", value or label)
        bm.create_block(new_label, b.value)
        bm.delete_block(b.id)
        return f"Renamed block {label} -> {new_label}"
    return f"Error: unknown action {action}"


def memory_apply_patch(ctx, label: str, patch: str) -> str:
    """简化 unified diff 应用（Letta 同款）。仅支持单块模式：
    @@ 行号上下文 + +/- 行。多块模式（*** Add Block: 等）返回明确错误（不实现）。"""
    bm: BlockManager = ctx.block_manager
    block = bm.get_block_by_label(label)
    if block is None:
        return f"Error: no block with label {label}"
    if "*** Add Block:" in patch or "*** Update Block:" in patch:
        return "Error: multi-block patch not supported"
    lines = block.value.splitlines(keepends=True)
    removals: list[str] = []
    additions: list[str] = []
    for line in patch.splitlines():
        if line.startswith("-"):
            removals.append(line[1:])
        elif line.startswith("+"):
            additions.append(line[1:])
    for r in removals:
        lines = [l for l in lines if l.rstrip("\n") != r]
    result = "".join(lines)
    if additions:
        result = result.rstrip("\n") + "\n" + "\n".join(additions) + "\n"
    try:
        bm.update_block_value(label, result)
    except ValueError as e:
        return f"Error: {e}"
    return f"Applied patch to block {label}"
