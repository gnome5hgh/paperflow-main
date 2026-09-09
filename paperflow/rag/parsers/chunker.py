"""AcademicChunker：把带章节结构的文档切成检索块。

两级切分：先按章节切，超长章节再按 token 数二次切分并带重叠。块 id 由
「相对路径 + 块序号」哈希而来、与内容无关，同一位置编辑后重切得到同一个
id，保证索引写入的幂等覆盖（对应 indexer 里的「先删后建」）。
"""
import hashlib
from dataclasses import dataclass

import tiktoken

#: 需丢弃的引用段标题前缀（中英文）。匹配这些标题的章节内容不进入检索块，
#: 因为参考文献列表对语义检索价值较低，且包含大量外部文献信息可能干扰检索。
_REFERENCE_HEADS = ("references", "参考文献", "bibliography")


@dataclass
class Chunk:
    """一个检索块：由切块器产出，包含文本、所属文档路径与章节信息。"""

    id: str            # 块唯一标识符，由 `sha1(相对路径 + 块序号 chunk_index)[:16]` 生成，
                       # 该 ID 与内容无关，同一文档位置重复切分得到相同 ID，编辑同一位置会得到同 id，保证了索引写入的幂等性（覆盖而非追加）。
    text: str          # 块的文本内容。
    path: str          # 文档相对于知识库根目录的路径（同时用作文档 id 与元数据，跨机器稳定）
    source: str        # 来源类型："note"（Markdown 笔记）| "pdf"
    heading: str       # 该块所属章节的标题（可能为空）。
    chunk_index: int   # 块在文档中的全局序号（从0开始），用于生成 ID。


class AcademicChunker:
    """两级切分：先按章节切，超长章节再按 token 数二次切分并带重叠。

    嵌入模型（Qwen3-Embedding-0.6B 支持 32K 上下文）对分块长度没有硬约束，max_tokens=512 是检索粒度的选择：块太大召回噪声多、太小语义碎片化；
    overlap 让相邻块重叠一部分，重叠让跨块语义连贯。token 计数用 cl100k_base 近似即可，不必精确。
    """

    def __init__(self, max_tokens: int = 512, overlap_tokens: int = 64):
        """配置分块参数并准备 token 计数器。

        Args:
            max_tokens: 每个块的最大 token 数。默认 512 留有余量，
                        避免接近模型输入上限 512 时被截断。
            overlap_tokens: 相邻块之间的重叠 token 数，用于保持跨块语义连贯。
                            默认 64 个 token，约为块长的 1/8。
        """
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        # cl100k_base：GPT-4 系列的 tokenizer，这里只需近似计数，不必精确
        self._enc = tiktoken.get_encoding("cl100k_base")

    def _is_reference(self, heading: str) -> bool:
        """判断章节标题是否为参考文献类标题（中英文）。此类内容不进入检索块。

        Args:
            heading: 章节标题字符串。

        Returns:
            bool: True 表示应跳过该章节，False 表示保留。
        """
        # 检查标题（去除首尾空白并转小写）是否以预定义的前缀开头。
        # 匹配的章节将被完全跳过，不进入检索块。
        return heading.strip().lower().startswith(_REFERENCE_HEADS)

    def _split_long(self, text: str) -> list[str]:
        """超长文本按 token 切分，步长 = max_tokens - overlap_tokens（保证重叠）。

        边界条件：
        - stride 至少为 1（当 overlap_tokens >= max_tokens 时，取 max(1, ...)）。
        - 重叠量实际可能略小于设定的 overlap_tokens（当剩余长度不足时）。
        - 解码后的片段可能因 token 边界导致首尾词汇被截断，但对检索影响较小。

        Args:
            text: 输入文本（通常是一个章节的完整内容）。

        Returns:
            list[str]: 分割后的文本片段列表。
        """
        # 1. 将文本编码为 token 序列。
        tokens = self._enc.encode(text)

        # 2. 若 token 数 <= max_tokens，直接返回原文本。
        if len(tokens) <= self.max_tokens:
            return [text]

        # 3. 否则，以步长 `stride = max_tokens - overlap_tokens` 滑动窗口，每次取 `max_tokens` 长度的 token 片段，解码回文本。
        stride = max(1, self.max_tokens - self.overlap_tokens)

        parts = []
        for start in range(0, len(tokens), stride):
            # 4. 最后一个片段可能不足 max_tokens，，正常截断，encode 可处理
            parts.append(self._enc.decode(tokens[start:start + self.max_tokens]))
        return parts

    def split_doc(self, rel_path: str, sections: list[tuple[str, str]], source: str) -> list[Chunk]:
        """把带章节结构的一篇文档切成 Chunk 列表，跳过参考文献章节。

        幂等性保证：
        - 同一文档的同一位置，由于切分逻辑和序号不变，生成的 ID 相同。
        - 当文档内容变化导致某些章节被删除或新增时，后续块的序号可能改变，
          但索引器采用“先删后建”策略，旧块会被显式删除，因此不会残留。

        Args:
            rel_path: 文档相对于知识库根目录的路径，用作块 ID 生成的一部分。
            sections: 章节列表，每个元素为 (标题, 正文) 的二元组。
            source: 来源类型，'note' 或 'pdf'。

        Returns:
            list[Chunk]: 生成的 Chunk 对象列表，按文档顺序排列。
        """
        chunks: list[Chunk] = []
        idx = 0  # 文档内全局块序号 id

        # 1. 遍历每个章节（标题, 正文）。
        for heading, text in sections:
            # 2. 若章节标题匹配参考文献前缀，则跳过整个章节。
            if self._is_reference(heading):
                continue
            # 3. 否则，对章节正文调用 `_split_long` 分割（可能返回一个或多个片段）。
            for part in self._split_long(text):
                # 4. 为每个片段生成一个 Chunk 对象，其中 id 由 `sha1(rel_path + 全局序号)[:16]` 生成。
                chunk_id = hashlib.sha1(f"{rel_path}:{idx}".encode()).hexdigest()[:16]
                chunks.append(Chunk(
                    id=chunk_id, text=part, path=rel_path,
                    source=source, heading=heading, chunk_index=idx,
                ))
                # 5. 全局序号 `idx` 从 0 开始递增，保证同一文档内不同位置的块 ID 唯一且稳定。
                idx += 1
        return chunks
