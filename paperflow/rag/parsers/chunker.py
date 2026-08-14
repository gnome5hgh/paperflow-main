"""AcademicChunker：把带章节结构的文档切成检索块。

两级切分：先按章节切，超长章节再按 token 数二次切分并带重叠。块 id 由
「相对路径 + 块序号」哈希而来、与内容无关，同一位置编辑后重切得到同一个
id，保证索引写入的幂等覆盖（对应 indexer 里的「先删后建」）。
"""
import hashlib
from dataclasses import dataclass

import tiktoken

#: 需丢弃的引用段标题前缀（中英文）
_REFERENCE_HEADS = ("references", "参考文献", "bibliography")


@dataclass
class Chunk:
    """一个检索块：由切块器产出，包含文本、所属文档路径与章节信息。"""

    id: str            # 块 id：sha1(路径 + 块序号)，内容无关、幂等，编辑同一位置会得到同 id
    text: str
    path: str          # 相对知识库根目录的路径（同时用作文档 id 与元数据，跨机器稳定）
    source: str        # 来源："note"（Markdown 笔记）| "pdf"
    heading: str
    chunk_index: int


class AcademicChunker:
    """两级切分：先按章节切，超长章节再按 token 数二次切分并带重叠。

    嵌入模型的最大输入长度正好是 512，顶满上限有被截断的风险，所以默认
    max_tokens=512 留出余量；overlap 让相邻块重叠一部分，保持上下文连贯。
    """

    def __init__(self, max_tokens: int = 512, overlap_tokens: int = 64):
        """配置分块参数并准备 token 计数器。

        max_tokens 是单块上限、overlap_tokens 是相邻块重叠量，两个默认值都
        有讲究：bge-small-zh-v1.5 的输入上限正好是 512，顶满有截断风险，
        留出余量；重叠让跨块语义连贯。token 计数用 cl100k_base 近似即可，
        不必精确。
        """
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        # cl100k_base：GPT-4 系列的 tokenizer，这里只需近似计数，不必精确
        self._enc = tiktoken.get_encoding("cl100k_base")

    def _is_reference(self, heading: str) -> bool:
        """判断章节标题是否为参考文献类标题（中英文）。此类内容不进入检索块。"""
        return heading.strip().lower().startswith(_REFERENCE_HEADS)

    def _split_long(self, text: str) -> list[str]:
        """超长文本按 token 切分，步长 = max_tokens - overlap_tokens（保证重叠）。"""
        tokens = self._enc.encode(text)
        if len(tokens) <= self.max_tokens:
            return [text]
        stride = max(1, self.max_tokens - self.overlap_tokens)
        parts = []
        for start in range(0, len(tokens), stride):
            parts.append(self._enc.decode(tokens[start:start + self.max_tokens]))
        return parts

    def split_doc(self, rel_path: str, sections: list[tuple[str, str]], source: str) -> list[Chunk]:
        """把带章节结构的一篇文档切成 Chunk 列表，跳过参考文献章节。

        每个块的 id 由路径加块序号哈希生成、与内容无关，保证同一位置
        重复切分得到相同 id（幂等）。
        """
        chunks: list[Chunk] = []
        idx = 0
        for heading, text in sections:
            if self._is_reference(heading):
                continue
            for part in self._split_long(text):
                chunk_id = hashlib.sha1(f"{rel_path}:{idx}".encode()).hexdigest()[:16]
                chunks.append(Chunk(
                    id=chunk_id, text=part, path=rel_path,
                    source=source, heading=heading, chunk_index=idx,
                ))
                idx += 1
        return chunks
