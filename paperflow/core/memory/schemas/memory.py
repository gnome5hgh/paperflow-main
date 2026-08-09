"""Letta Memory（core memory 块容器），命名对齐 letta/schemas/memory.py。

compile() 按 MemFS 渐进暴露：只渲染 system/ 目录下的块（persona/human），
非 system/ 块不进 compile（索引常驻、内容按需读取）。
"""
from __future__ import annotations

from paperflow.core.memory.schemas.block import Block

__all__ = ["Memory"]


class Memory:
    def __init__(self, blocks: list[Block] | None = None):
        self.blocks: list[Block] = list(blocks or [])

    def compile(self) -> str:
        system_blocks = [b for b in self.blocks if b.label in ("persona", "human")]
        parts = ["<memory_blocks>"]
        for b in system_blocks:
            parts.append(f'<block name="{b.label}">{b.value}</block>')
        parts.append("</memory_blocks>")
        return "\n".join(parts)

    def get_block(self, label: str) -> Block | None:
        for b in self.blocks:
            if b.label == label:
                return b
        return None

    def create_block(self, label: str | None = None, value: str = "", **kwargs) -> Block:
        b = Block(label=label, value=value, **kwargs)
        self.blocks.append(b)
        return b

    def update_block_value(self, label: str, value: str) -> Block:
        b = self.get_block(label)
        if b is None:
            raise ValueError(f"no block with label {label}")
        b.value = value
        return b

    def set_block(self, block: Block) -> None:
        for i, b in enumerate(self.blocks):
            if b.label == block.label:
                self.blocks[i] = block
                return
        self.blocks.append(block)

    def get_blocks(self) -> list[Block]:
        return list(self.blocks)
