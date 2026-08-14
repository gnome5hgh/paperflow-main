"""Memory：核心记忆块容器，负责把块编译成 LLM 可读的 system 文本。

compile() 按「渐进暴露」原则：只把 persona/human 两块内容常驻渲染进
<memory_blocks>，其余块不进 compile——它们以可选的 index_text（文件树索引）
形式出现，内容按需读取。这样 in-context 窗口只装常驻核心，全量记忆走
MemFS 的文件树隐喻触达。
"""
from __future__ import annotations

from paperflow.core.memory.schemas.block import Block

__all__ = ["Memory"]


class Memory:
    """核心记忆块容器：持有块列表，提供编译与增删改查。

    不是 pydantic 模型而是普通容器类，供 AgentState 等嵌套持有。
    """

    def __init__(self, blocks: list[Block] | None = None):
        self.blocks: list[Block] = list(blocks or [])

    def compile(self, index_text: str | None = None) -> str:
        """渲染核心记忆为 system 文本：system/ 块 + 可选的文件系统索引。

        渐进暴露按「persona/human 两块内容常驻；非 system 块只以索引形式
        出现」的原则。index_text 由调用方（Agent._memory_message）读取并传入，
        保持本类无文件 IO。
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
        """按 label 取块；不存在返回 None。"""
        for b in self.blocks:
            if b.label == label:
                return b
        return None

    def create_block(self, label: str | None = None, value: str = "", **kwargs) -> Block:
        """新建一个块并加入容器（内存态；落盘由 BlockManager 负责）。"""
        b = Block(label=label, value=value, **kwargs)
        self.blocks.append(b)
        return b

    def update_block_value(self, label: str, value: str) -> Block:
        """就地更新指定 label 块的值；label 不存在抛 ValueError。"""
        b = self.get_block(label)
        if b is None:
            raise ValueError(f"no block with label {label}")
        b.value = value
        return b

    def set_block(self, block: Block) -> None:
        """按 label 覆盖容器中的块；不存在则追加。"""
        for i, b in enumerate(self.blocks):
            if b.label == block.label:
                self.blocks[i] = block
                return
        self.blocks.append(block)

    def get_blocks(self) -> list[Block]:
        """返回块列表的副本（防止调用方就地污染容器）。"""
        return list(self.blocks)
