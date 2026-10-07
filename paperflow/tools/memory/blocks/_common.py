"""blocks 组共享块操作 helper（memory_rethink 与 memory 的 replace 动作共用）。"""
__all__ = ["rewrite_block"]


def rewrite_block(bm, label: str, new_memory: str) -> str:
    """整块重写：写失败（read_only / 超限）返回错误文本，块缺失抛 KeyError。

    经 mutate_block 原子写入（整块替换也是「读-改-写」，统一走同一入口）。
    """
    try:
        bm.mutate_block(label, lambda v: new_memory)
    except ValueError as e:
        return f"Error: {e}"
    return f"Rewrote block {label}"
