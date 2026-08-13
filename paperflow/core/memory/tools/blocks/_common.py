"""blocks 组共享块操作 helper（memory_rethink 与 memory 的 replace 动作共用）。"""
__all__ = ["rewrite_block"]


def rewrite_block(bm, label: str, new_memory: str) -> str:
    """整块重写：update_block_value 失败（如 read_only）返回错误文本。"""
    try:
        bm.update_block_value(label, new_memory)
    except ValueError as e:
        return f"Error: {e}"
    return f"Rewrote block {label}"
