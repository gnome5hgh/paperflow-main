"""AcademicChunker：把带章节结构的文档切成检索块。

两级切分：先按章节切，超长章节再按 token 数二次切分并带重叠。块 id 由
「相对路径 + 块序号」哈希而来、与内容无关，同一位置编辑后重切得到同一个
id，保证索引写入的幂等覆盖（对应 indexer 里的「先删后建」）。
"""
import hashlib
import re
from dataclasses import dataclass

import tiktoken

#: 需丢弃的引用段标题前缀（中英文）。匹配这些标题的章节内容不进入检索块，
#: 因为参考文献列表对语义检索价值较低，且包含大量外部文献信息可能干扰检索。
_REFERENCE_HEADS = ("references", "参考文献", "bibliography")

#: 句子边界：中英句末标点（。！？!?）之后，或英文句点后跟空白处。
#: 小数（3.5）与缩写（et al.）会误断，可接受——只影响窗口边界位置，不影响内容完整性。
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s*|(?<=\.)\s+")


def context_prefix(title: str, heading: str, body: str) -> str:
    """把「论文标题 > 章节标题」前缀行拼到块正文前。

    前缀随块文本同时进入 embedding、BM25 与检索展示（Anthropic contextual
    retrieval 的标题路径版：任一块被单独检回时都自带所属论文与章节）。
    标题与章节都缺省时返回原正文（PyMuPDF 回退与无标题笔记兼容）。
    """
    label = " > ".join(x for x in (title.strip(), heading.strip()) if x)
    return f"{label}\n{body}" if label else body


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

    def _split_sentences(self, text: str) -> list[str]:
        """按句末标点把文本切成句子列表（过滤纯空白片段）。"""
        return [p for p in _SENT_SPLIT_RE.split(text) if p and p.strip()]

    def _token_window(self, tokens: list[int]) -> list[str]:
        """对 token 序列做硬滑窗（步长 = max_tokens - overlap_tokens）。

        只用于两个回退场景：全文不足两句但超预算、单句超长。
        """
        stride = max(1, self.max_tokens - self.overlap_tokens)
        return [self._enc.decode(tokens[start:start + self.max_tokens])
                for start in range(0, len(tokens), stride)]

    def _pack_sentences(self, sentences: list[str]) -> list[str]:
        """按句装窗：逐句累加，超过 max_tokens 封窗。

        新窗以「上一窗尾部约 overlap_tokens 的完整句子」开头；凑不出完整句、
        或重叠句加上当前句会超预算时放弃重叠（宁少重叠，不超预算、不切句）。
        """
        windows: list[str] = []
        cur: list[str] = []
        cur_len = 0
        for sent in sentences:
            t = len(self._enc.encode(sent))
            if cur and cur_len + t > self.max_tokens:
                windows.append("".join(cur))
                overlap: list[str] = []
                overlap_len = 0
                for prev in reversed(cur):
                    pt = len(self._enc.encode(prev))
                    if overlap and overlap_len + pt > self.overlap_tokens:
                        break
                    overlap.insert(0, prev)
                    overlap_len += pt
                if overlap_len + t > self.max_tokens:
                    overlap, overlap_len = [], 0
                cur, cur_len = overlap, overlap_len
            if t > self.max_tokens:
                # 单句超长：唯一允许切句的场景，回退 token 硬滑窗
                if cur:
                    windows.append("".join(cur))
                    cur, cur_len = [], 0
                windows.extend(self._token_window(self._enc.encode(sent)))
                continue
            cur.append(sent)
            cur_len += t
        if cur:
            windows.append("".join(cur))
        return windows

    def _split_long(self, text: str) -> list[str]:
        """超长文本切分：先按句切再按 token 预算装窗（不切句）。

        回退：全文不足两句但超预算、或单句超长时用 token 硬滑窗。
        """
        if len(self._enc.encode(text)) <= self.max_tokens:
            return [text]
        sentences = self._split_sentences(text)
        if len(sentences) <= 1:
            return self._token_window(self._enc.encode(text))
        return self._pack_sentences(sentences)

    def split_doc(self, rel_path: str, sections: list[tuple[str, str]], source: str,
                  title: str = "") -> list[Chunk]:
        """把带章节结构的一篇文档切成 Chunk 列表，跳过参考文献章节。

        Args:
            title: 文档标题（PDF=GROBID 主标题，笔记=H1）；与 heading 一起拼成
                   每个窗口的首行前缀，随文本进入 embedding/BM25/展示。

        其余语义（幂等 id、先删后建配合）与原实现一致，见类注释。
        """
        chunks: list[Chunk] = []
        idx = 0  # 文档内全局块序号 id

        # 1. 遍历每个章节（标题, 正文）。
        for heading, text in sections:
            # 2. 若章节标题匹配参考文献前缀，则跳过整个章节。
            if self._is_reference(heading):
                continue
            # 3. 否则，对章节正文调用 `_split_long` 分割（可能返回一个或多个片段）。
            # 4. 前缀逐窗拼接（而非拼进原文再切）：长章节切多窗时每个窗口都自带
            #    「标题 > 章节」上下文，任一窗口被单独检回都不丢所属信息。
            for part in self._split_long(text):
                # 5. 为每个片段生成一个 Chunk 对象，其中 id 由 `sha1(rel_path + 全局序号)[:16]` 生成。
                chunk_id = hashlib.sha1(f"{rel_path}:{idx}".encode()).hexdigest()[:16]
                chunks.append(Chunk(
                    id=chunk_id, text=context_prefix(title, heading, part),
                    path=rel_path, source=source, heading=heading, chunk_index=idx,
                ))
                # 6. 全局序号 `idx` 从 0 开始递增，保证同一文档内不同位置的块 ID 唯一且稳定。
                idx += 1
        return chunks
