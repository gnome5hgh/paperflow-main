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

    职责：
        - 存储内存态的所有 Block 对象（运行时快照）。
        - 提供编译为 system prompt 文本的能力（渐进暴露）。
        - 提供对块的基本 CRUD 操作（仅内存态，不直接持久化）。

    注意：
        - 本类不负责持久化（落盘由 BlockManager 负责），
          其 blocks 列表应视为 BlockManager 的缓存副本，保持最终一致。
        - 所有修改操作（create/update/set）只影响内存中的列表，
          调用方需在适当时机通过 BlockManager 将变更持久化到数据库。
    """

    def __init__(self, blocks: list[Block] | None = None):
        """初始化 Memory 容器。

        Args:
            blocks: 可选的初始块列表。若未提供，则初始化为空列表。
        """
        self.blocks: list[Block] = list(blocks or [])

    def compile(self, index_text: str | None = None) -> str:
        """渲染核心记忆为 system 文本：system/ 块 + 可选的文件系统索引。

        渐进暴露按「persona/human 两块内容常驻；非 system 块只以索引形式
        出现」的原则。index_text 由调用方（Agent._memory_message）读取并传入，
        保持本类无文件 IO。

        Args:
            index_text: MemFS 自动生成的 memory_filesystem.md 内容（文件树索引）。
                若为 None 或空，则不添加 <memory_filesystem> 标签。

        Returns:
            完整的 system prompt 文本，结构如下：
            <memory_blocks>
            <block name="persona">...内容...</block>
            <block name="human">...内容...</block>
            </memory_blocks>
            [可选] <memory_filesystem>...索引内容...</memory_filesystem>

        设计意图：
            - 常驻核心块（persona/human）每轮都完整注入，保障基本身份和用户画像。
            - 其他块（如 unread_list, history_list 等）不直接包含内容，只通过
              文件树索引暴露存在性，LLM 可按需读取具体文件内容。
            - 这样在上下文窗口中节省大量 token，同时保持全量记忆可访问。
        """
        # 筛选出 system 核心块（persona 和 human）
        system_blocks = [b for b in self.blocks if b.label in ("persona", "human")]
        parts = ["<memory_blocks>"]
        for b in system_blocks:
            parts.append(f'<block name="{b.label}">{b.value}</block>')
        parts.append("</memory_blocks>")
        # 若提供了索引文本，追加文件系统索引块
        if index_text:
            parts.append("<memory_filesystem>")
            parts.append(index_text)
            parts.append("</memory_filesystem>")
        return "\n".join(parts)

    def get_block(self, label: str) -> Block | None:
        """按 label 取块；不存在返回 None。

        Args:
            label: 块的标签名称（如 "persona"）。

        Returns:
            匹配的第一个 Block 对象，若 label 不匹配任何块则返回 None。

        注意：label 在业务上应是唯一的，但未强制唯一约束，若存在多个相同 label，
        只返回第一个匹配的块。建议调用方保证 label 唯一。
        """
        for b in self.blocks:
            if b.label == label:
                return b
        return None

    def create_block(self, label: str | None = None, value: str = "", **kwargs) -> Block:
        """新建一个块并加入容器（内存态；落盘由 BlockManager 负责）。

        Args:
            label: 块的标签（可选，若为 None 则 Block 构造时 label 为 None）。
            value: 块的文本内容（默认为空字符串）。
            **kwargs: 传递给 Block 构造函数的其他参数（如 limit, description,
                metadata_, read_only 等）。

        Returns:
            新创建的 Block 实例（已加入到 self.blocks 列表中）。

        注意：
            - 该方法仅修改内存列表，不写数据库。
            - 块 ID 由 Block 的 `_block_id` 自动生成（若未显式提供）。
            - 若调用方希望后续持久化，需使用 BlockManager.create_block 或
              在创建后调用 BlockManager 的同步方法。
        """
        b = Block(label=label, value=value, **kwargs)
        self.blocks.append(b)
        return b

    def update_block_value(self, label: str, value: str) -> Block:
        """就地更新指定 label 块的值；label 不存在抛 ValueError。

        Args:
            label: 目标块的标签。
            value: 新的文本内容。

        Returns:
            更新后的 Block 对象（引用）。

        Raises:
            ValueError: 当不存在 label 匹配的块时。

        注意：
            - 此方法直接修改内存中的 Block 对象，不会触发版本递增或历史快照
              （这些操作由 BlockManager 负责）。
            - 若需要同时更新其他字段（如 description），应直接获取 Block 对象
              并修改其属性，然后调用 BlockManager 持久化。
        """
        b = self.get_block(label)
        if b is None:
            raise ValueError(f"no block with label {label}")
        b.value = value
        return b

    def set_block(self, block: Block) -> None:
        """按 label 覆盖容器中的块；不存在则追加。

        Args:
            block: 要设置的 Block 对象。

        行为：
            - 若容器中存在与 block.label 相同的块，则替换为该 block 实例。
            - 若不存在，则直接追加到列表末尾。

        用途：用于同步外部（如 BlockManager）的块状态到内存容器，确保一致。
        """
        for i, b in enumerate(self.blocks):
            if b.label == block.label:
                self.blocks[i] = block
                return
        self.blocks.append(block)

    def get_blocks(self) -> list[Block]:
        """返回块列表的副本（防止调用方就地污染容器）。

        Returns:
            当前块列表的浅拷贝（list copy）。

        设计考虑：
            - 返回副本可防止外部代码意外修改内部列表结构（如添加/删除元素）。
            - 但返回的是 Block 对象引用的副本，若调用方修改 Block 自身的属性，
              仍会影响容器中的对象。若需深拷贝，调用方应自行处理。
        """
        return list(self.blocks)