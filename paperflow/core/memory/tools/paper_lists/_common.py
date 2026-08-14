"""paper_lists 组共享清单块操作（论文清单块的行级增删 helper）。

append = 只追加不改旧（读旧值 + 拼新行）；目标块缺失先建（写入意图可自动建）。
remove 按行首 `- {key}` 前缀匹配删行，找不到返回明确错误不静默；块缺失时直接
报 not found、不物化空块——删除不该创造任何状态，为删除物化空块是 MemFS 污染。
"""
__all__ = ["ensure_block", "append_line", "remove_line_by_key"]


def ensure_block(bm, block_label: str) -> None:
    """目标块缺失时创建（append 的自动建块入口）。"""
    if bm.get_block_by_label(block_label) is None:
        bm.create_block(block_label, "")


def append_line(bm, block_label: str, line: str) -> str:
    """在清单块追加一行（只追加不改旧，同论文可多次追加靠时间/动作区分）。"""
    ensure_block(bm, block_label)
    block = bm.get_block_by_label(block_label)
    value = block.value.strip()
    new_value = f"{value}\n{line}" if value else line
    bm.update_block_value(block_label, new_value)
    return f"Appended to {block_label}"


def remove_line_by_key(bm, block_label: str, key: str) -> str:
    """按行首 `- {key}` 前缀匹配删行；空 key 或找不到返回错误文本。

    行形如 `- 标题 (来源)`，用 startswith 前缀匹配而非整行全等——行尾元数据
    （来源等）不受影响；无命中返回显式错误不静默。
    """
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
