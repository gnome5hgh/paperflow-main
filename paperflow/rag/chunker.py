"""AcademicChunker：按 section 切块，超长 section 二级 token 切分（带重叠）。"""
import hashlib
from dataclasses import dataclass

import tiktoken

#: 需丢弃的引用段标题前缀（中英文）
_REFERENCE_HEADS = ("references", "参考文献", "bibliography")


@dataclass
class Chunk:
    id: str            # sha1(rel_path + chunk_index)，幂等
    text: str
    path: str          # 相对 vault 根（doc id 与 metadata 共用，跨机器稳定）
    source: str        # "note" | "pdf"
    heading: str
    chunk_index: int


class AcademicChunker:
    """两级切分：先按 section，超长 section 再按 max_tokens + 重叠切。

    bge max_seq_length 恰为 512，顶到上限有截断风险，故默认 max_tokens=512
    留余量 + overlap 保持上下文连贯。"""

    def __init__(self, max_tokens: int = 512, overlap_tokens: int = 64):
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        # cl100k_base：GPT-4 系 tokenizer，近似计数即可
        self._enc = tiktoken.get_encoding("cl100k_base")

    def _is_reference(self, heading: str) -> bool:
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
