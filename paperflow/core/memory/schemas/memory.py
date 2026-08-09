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

    def compile(self, index_text: str | None = None) -> str:
        """渲染核心记忆为 system 文本：system/ 块 + 可选的文件系统索引。

        渐进暴露按 MemFS 语义：persona/human 两块内容常驻；非 system 块只以
        index_text（memory_filesystem.md 的文件树索引）形式出现,内容按需读取。
        index_text 由调用方（Agent._memory_message）读取并传入,保持本类无文件 IO。
        """
        system_blocks = [b for b in self.blocks if b.label in ("persona", "human")]
        parts = ["<memory_blocks>"]
        for b in system_blocks:
            parts.append(f'<block name="{b.label}">{b.value}</block>')
        parts.append("</memory_blocks>")
        if index_text:
            parts.append("<memory_filesystem>")
            parts.append(index_text)
            parts.append("</memory_filesystem>")
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
