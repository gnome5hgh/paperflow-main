"""paper_lists 组共享清单块操作（原 ListBlockTool 基类逻辑改为模块函数）。

append = 只追加不改旧（读旧值 + 拼新行）；块缺失先建（create_block → 自动更新
memory_filesystem.md 索引）。remove 按行首 `- {key}` 前缀匹配删行，找不到返回
明确错误不静默；块缺失时直接 not found、不物化空块（避免 MemFS 污染）。
"""
__all__ = ["ensure_block", "append_line", "remove_line_by_key"]


def ensure_block(bm, block_label: str) -> None:
    """目标块缺失时创建。"""
    if bm.get_block_by_label(block_label) is None:
        bm.create_block(block_label, "")


def append_line(bm, block_label: str, line: str) -> str:
    """在清单块追加一行（只追加不改旧）。"""
    ensure_block(bm, block_label)
    block = bm.get_block_by_label(block_label)
    value = block.value.strip()
    new_value = f"{value}\n{line}" if value else line
    bm.update_block_value(block_label, new_value)
    return f"Appended to {block_label}"


def remove_line_by_key(bm, block_label: str, key: str) -> str:
    """按行首 `- {key}` 前缀匹配删行；空 key 或找不到返回错误文本。"""
    if not key.strip():
        return "Error: empty title for removal"
    block = bm.get_block_by_label(block_label)
    if block is None:
        return f"Error: '{key}' not found in {block_label}"
    prefix = f"- {key}"
    lines = [ln for ln in block.value.splitlines() if ln.strip()]
    kept = [ln for ln in lines if not ln.startswith(prefix)]
    if len(kept) == len(lines):
        return f"Error: '{key}' not found in {block_label}"
    bm.update_block_value(block_label, "\n".join(kept))
    return f"Removed from {block_label}"
