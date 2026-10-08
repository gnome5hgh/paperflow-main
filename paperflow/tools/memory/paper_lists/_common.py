"""paper_lists 组共享清单块操作（论文清单块的行级增删 helper）。

append = 只追加不改旧（原子追加，读旧值 + 拼新行收进一次持锁的 mutator）；
目标块缺失先建（写入意图可自动建）。remove 按行首 `- {key}` 前缀匹配删行，
找不到返回明确错误不静默；块缺失时直接报 not found、不物化空块——删除不该
创造任何状态，为删除物化空块是 MemFS 污染。
"""
__all__ = ["ensure_block", "append_line", "remove_line_by_key"]

#: 建块时预置的 markdown 标题——MemFS 投影「正文==块值」的比对机制不被破坏，
#: 标题进块值（方案 a），人工阅读/手改体验更好。
_BLOCK_TITLES = {
    "unread_list": "# 待读清单",
    "history_list": "# 浏览历史",
}


def ensure_block(bm, block_label: str) -> None:
    """目标块缺失时创建（append 的自动建块入口），清单类块预置标题行。

    Args:
        bm: BlockManager，块 CRUD 服务
        block_label: str，清单块标签
    """
    if bm.get_block_by_label(block_label) is None:
        bm.create_block(block_label, _BLOCK_TITLES.get(block_label, ""))


def append_line(bm, block_label: str, line: str) -> str:
    """在清单块追加一行（只追加不改旧，同论文可多次追加靠时间/动作区分）。

    追加走 mutate_block：把「读旧值 → 拼新行 → 写回」收进一次持锁的原子操作，
    并发追加不会互相抹掉。

    Args:
        bm: BlockManager，块 CRUD 服务
        block_label: str，清单块标签
        line: str，待追加的行

    Returns:
        成功返回 "Appended to <label>"。
    """
    ensure_block(bm, block_label)
    bm.mutate_block(block_label,
                    lambda v: f"{v.strip()}\n{line}" if v.strip() else line)
    return f"Appended to {block_label}"


def remove_line_by_key(bm, block_label: str, key: str) -> str:
    """按行首 `- {key}` 前缀匹配删行；空 key 或找不到返回错误文本。

    行形如 `- 标题 (来源)`，用 startswith 前缀匹配而非整行全等——行尾元数据
    （来源等）不受影响；无命中返回显式错误不静默。删行走 mutate_block：整段
    读-算-写持锁完成，且删行判定（未命中返回 None）与写入在同一次操作里。

    Args:
        bm: BlockManager，块 CRUD 服务
        block_label: str，清单块标签
        key: str，用于前缀匹配的标题

    Returns:
        成功返回 "Removed from <label>"；空 key/未命中/块缺失返回错误文本。
    """
    if not key.strip():
        return "Error: empty title for removal"
    prefix = f"- {key}"

    def _drop(v: str) -> str | None:
        """去掉所有以 `- {key}` 开头的行；一行都没删掉时返回 None 表示未命中。

        Args:
            v: str，块的当前值（持锁内的最新值）

        Returns:
            去掉所有匹配行后的新值；一行都没删掉时返回 None（未命中）。
        """
        lines = [ln for ln in v.splitlines() if ln.strip()]
        kept = [ln for ln in lines if not ln.startswith(prefix)]
        return None if len(kept) == len(lines) else "\n".join(kept)

    try:
        if bm.mutate_block(block_label, _drop) is None:
            return f"Error: '{key}' not found in {block_label}"
    except KeyError:
        return f"Error: '{key}' not found in {block_label}"
    return f"Removed from {block_label}"
