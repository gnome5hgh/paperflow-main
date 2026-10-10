"""AcademicChunker：把带章节结构的文档切成检索块。

两级切分：先按章节切，超长章节再按 token 数二次切分并带重叠。块 id 由
「绝对路径 + 块序号」哈希而来、与内容无关，同一位置编辑后重切得到同一个
id，保证索引写入的幂等覆盖（对应 indexer 里的「先删后建」）。
"""
import hashlib
import re

from paperflow.core.common.tokenization import get_token_encoder
from paperflow.rag.constants import CHUNK_ID_LEN
from paperflow.rag.domain import Chunk, Section

#: 需丢弃的引用段标题前缀（中英文）。匹配这些标题的章节内容不进入检索块，
#: 因为参考文献列表对语义检索价值较低，且包含大量外部文献信息可能干扰检索。
_REFERENCE_HEADS = ("references", "参考文献", "bibliography")

#: 句子边界：中英句末标点（。！？!?）之后，或英文句点后跟空白处。
#: 小数（3.5）与缩写（et al.）会误断，可接受——只影响窗口边界位置，不影响内容完整性。
_SENT_SPLIT_RE = re.compile(r"(?<=[。！？!?])\s*|(?<=\.)\s+")

#: 句末标点字符集（中英）。与 _SENT_SPLIT_RE 用途不同：它不切句，只回答
#: 「这段正文里有没有完整句子」，供残渣判据使用。ASCII 句点必须算在内，
#: 否则英文正文会被整体误判为无句。
_SENT_END_RE = re.compile(r"[。！？!?.]")

#: 需整段丢弃的期刊样板章节标题词干（小写子串匹配）。致谢 / 资助 / 利益冲突 /
#: 数据可用性声明等出版流程产物对学术检索零价值，却以「标题 + 一句话」的形态
#: 成为碎块参与检索排序、挤占 top-k。与 _REFERENCE_HEADS 同一机制、同一后果：
#: 命中即整段跳过。词干刻意选多词短语并做子串匹配——样板标题形态多样
#: （"Declaration of competing interest" / "Competing interests" / "■ Acknowledgments"），
#: 而 conclusions / datasets / methodology 等真实章节不得命中。
_DROP_HEADS = (
    "data availability", "availability of data", "funding",
    "competing interest", "competing financial", "conflict of interest",
    "acknowledg", "author contribution", "authorship contribution",
    "declaration", "supplementar", "publisher", "consent",
    "ethic", "copyright", "license", "orcid", "peer review",
)

#: 正文残渣判据的 token 上限：低于它且不含任何句末标点的正文视为解析残渣
#: （公式碎片、表格单元格拼接、被截断的空洞句）。阈值刻意保守——无句点的
#: 稍长正文常是被解析截断的真实内容，放宽会成片误伤；只收最没有检索价值的微碎片。
_FRAGMENT_MAX_TOKENS = 12


class AcademicChunker:
    """两级切分：先按章节切，超长章节再按 token 数二次切分并带重叠。

    切块前先做丢弃判据（参考文献 / 期刊样板 / 解析残渣，见 split_doc），
    被丢弃的章节不产生块。嵌入模型（Qwen3-Embedding-0.6B 支持 32K 上下文）
    对分块长度没有硬约束，max_tokens=512 是检索粒度的选择：块太大召回噪声多、
    太小语义碎片化；overlap 让相邻块重叠一部分，重叠让跨块语义连贯。token
    计数用 ``core.common.tokenization.TOKEN_ENCODING``（与 core.memory 压缩共用）
    近似即可，不必精确。

    Attributes:
        max_tokens: int，每个块的最大 token 数（检索粒度选择，非模型硬约束）
        overlap_tokens: int，相邻块的重叠 token 数（保持跨块语义连贯）
        _enc: token 计数器（core.common.tokenization 单点共享，近似计数即可）
    """

    def __init__(self, max_tokens: int, overlap_tokens: int):
        """配置分块参数并准备 token 计数器。

        生产值来自 ``config.rag.chunker.*``（唯一声明点 config.py，RagService
        装配注入；改默认值触发配方哈希全量重索引）。

        Args:
            max_tokens: 每个块的最大 token 数。
            overlap_tokens: 相邻块之间的重叠 token 数，用于保持跨块语义连贯。
        """
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        # 编码器口径单点共享（core.common.tokenization/get_token_encoder），与
        # core.memory 压缩共用；只需近似计数，不必精确。
        self._enc = get_token_encoder()

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

    def _is_boilerplate(self, heading: str) -> bool:
        """判断章节标题是否为期刊样板段（致谢/资助/利益冲突/数据可用性等）。

        与 _is_reference 同一机制、同一后果：整段跳过，不进入检索块。用子串
        而非前缀匹配，因为样板标题形态多样（"Declaration of competing
        interest" / "Competing interests" / "■ Acknowledgments"）。

        Args:
            heading: 章节标题字符串。

        Returns:
            bool: True 表示应跳过该章节，False 表示保留。
        """
        low = heading.strip().lower()
        return any(stem in low for stem in _DROP_HEADS)

    def _is_fragment(self, text: str) -> bool:
        """判断章节正文是否为解析残渣：不成句的微碎片或空正文。

        拦下两类东西——解析器把公式/表格单元格切出的无句点碎片，以及只有
        标题没有正文的空章节。判据刻意保守：仅当正文低于 _FRAGMENT_MAX_TOKENS
        且不含任何句末标点才判残渣；无句点的稍长正文常是被截断的真实内容，
        不在此列。

        Args:
            text: 章节正文。

        Returns:
            bool: True 表示应跳过该章节，False 表示保留。
        """
        if not text.strip():
            return True
        if len(self._enc.encode(text)) >= _FRAGMENT_MAX_TOKENS:
            return False
        return not _SENT_END_RE.search(text)

    def _split_sentences(self, text: str) -> list[str]:
        """按句末标点把文本切成句子列表（过滤纯空白片段）。

        Args:
            text: 待切分的文本。

        Returns:
            句子列表（保留句末标点，元素非空）。
        """
        return [p for p in _SENT_SPLIT_RE.split(text) if p and p.strip()]

    def _token_window(self, tokens: list[int]) -> list[str]:
        """对 token 序列做硬滑窗（步长 = max_tokens - overlap_tokens）。

        用于：1、全文超预算但切不出两句（比如一整段没句号）
             2、_pack_sentences——单个句子自己超过整个预算

        Args:
            tokens: encode 后的 token id 序列。

        Returns:
            解码回文本的窗口列表，每窗 token 数恰好 max_tokens（末窗除外）。
        """
        stride = max(1, self.max_tokens - self.overlap_tokens)
        return [self._enc.decode(tokens[start:start + self.max_tokens])
                for start in range(0, len(tokens), stride)]

    def _pack_sentences(self, sentences: list[str]) -> list[str]:
        """按句装窗：逐句累加，超过 max_tokens 封窗。……

        新窗以「上一窗尾部约 overlap_tokens 的完整句子」开头；凑不出完整句、
        或重叠句加上当前句会超预算时放弃重叠（宁少重叠，不超预算、不切句）。

        Args:
            sentences: 句子列表，须由 _split_sentences 切出（句末标点边界、
                       已滤空白片段、元素非空）。顺序即原文顺序——窗口重叠
                       的"尾部回收集句"依赖此顺序保证语义连贯。
                       单个句子本身可超 max_tokens：该句在循环内单独回退
                       token 硬滑窗（唯一切句场景），不影响其余句子的按句装窗。

        Returns:
            list[str]: 封好的窗口文本列表，顺序与输入句子顺序一致、内容无遗漏
                       （每句恰好属于一个窗口，或经硬滑窗拆进多个连续窗口；
                       重叠使相邻窗口共享句子，但重叠句同时位于两窗是刻意设计）。
                       每窗 token 数 ≤ max_tokens（重叠句计入下一窗预算）；
                       空输入返回空列表。窗口文本为句子直接拼接（无分隔符），
                       不含「标题 > 章节」前缀——前缀由调用方 split_doc 逐窗拼接。
        """

        windows: list[str] = []   # 已封窗的成品
        cur: list[str] = []       # 当前正在攒的窗（保存攒着的句子）
        cur_len = 0               # 当前窗的 token 总数（缓存，避免反复 encode 整窗）

        # 遍历一个章节的全部句子
        for sent in sentences:
            # 本句的 token 数（每句 encode 一次）
            t = len(self._enc.encode(sent))

            # ---- 封窗判定：当前窗非空，且装下本句会超预算则开始封窗 ----
            if cur and cur_len + t > self.max_tokens:
                # 封窗包含四步骤：
                # ① 当前正在攒的窗拼接入成品
                windows.append("".join(cur))

                # ② 从当前窗【尾部】往回收集“重叠句”：
                #    逆序遍历，逐句往前插（insert(0, prev) 保持原顺序），
                #    直到再加一句就会超过 overlap_tokens 为止。
                #    注意条件里的 `overlap and`：第一句无条件收——即使它自己
                #    就超过 overlap 预算也先收着（好过没有重叠），
                #    这是"宁少重叠"而非"零重叠"的体现
                overlap: list[str] = []
                overlap_len = 0
                for prev in reversed(cur):
                    pt = len(self._enc.encode(prev)) # 当前遍历到的句子的 token 数
                    # if overlap 表示 overlap 为空时就不满足条件，
                    # 那么逆序遍历到的第一句永远收，即使这个第一句本身的 token 数就超过 overlap_tokens
                    # overlap_len + pt > self.overlap_tokens 表示再加上当前的句子就会超过 overlap_tokens，则 break，不处理当前句子
                    if overlap and overlap_len + pt > self.overlap_tokens:
                        break

                    # 将当前句子加入“重叠句”
                    overlap.insert(0, prev)
                    overlap_len += pt

                # ③ 重叠可行性检查：重叠句 + 本句如果已经超出块的最大 token 预算，
                #    说明上一窗尾部是一句超大的话——放弃重叠，新窗从本句干净起步
                #    （宁少重叠，不超预算：重叠是锦上添花，预算是硬约束）
                if overlap_len + t > self.max_tokens:
                    overlap, overlap_len = [], 0

                # ④ 通过了重叠可行性检查：新窗从"上一窗的尾部句子"开始
                cur, cur_len = overlap, overlap_len

            # ---- 单句超出块的最大 token 预算：唯一允许切句的场景 ----
            # 走 token 硬滑窗把这一句单独切小；若当前窗里已有句子，先封掉。
            # （注意此判定在封窗之后：因此上面刚攒好的 overlap 若非空，
            #   会先被当作独立小窗封出去——边界行为上的小冗余，语义无害）
            if t > self.max_tokens:
                if cur:
                    windows.append("".join(cur))
                    cur, cur_len = [], 0
                windows.extend(self._token_window(self._enc.encode(sent)))
                continue                              # 本句已被消费，跳过入窗

            # ---- 常规：句子入窗，累加长度 ----
            cur.append(sent)
            cur_len += t

        # ---- 收尾：最后一批句子不足一窗（没触发过封窗），也要封出去 ----
        if cur:
            windows.append("".join(cur))
        return windows

    def _split_long(self, text: str) -> list[str]:
        """章节级：判断"切不切、怎么切"的调度器。

        超长文本切分：先按句切再按 token 预算装窗（不切句）。

        回退：全文不足两句但超预算、或单句超长时用 token 硬滑窗。

        Args:
            text: 单个章节的正文文本（不含「标题 > 章节」前缀，前缀由调用方 split_doc 逐窗拼接）。

        Returns:
            list[str]: 切分后的窗口文本列表。每个窗口 token 数不超过 max_tokens 硬滑窗路径下严格相等，按句路径下 ≤）；
                       未超预算时为只含原文一个元素的列表。窗口可能为空列表——
                       text 去除空白后无内容时各分支均无可切之物（调用方 split_doc 侧
                       不做二次过滤，索引侧 index_document 有空白块过滤兜底）。
        """
        # ---- 分支①：没超预算，不值得切，原文整段返回 ----
        # 用 token 计数（不是字符数）做判断——预算是给嵌入模型的输入长度定的，
        # 中英文 token 密度差异大，字符数判断会失真
        if len(self._enc.encode(text)) <= self.max_tokens:
            return [text]

        # ---- 超预算：先试着按句切 ----
        # _split_sentences 用句末标点（。！？!? / 英文句点+空白）的正则切分，
        # 过滤纯空白片段，得到句子列表
        sentences = self._split_sentences(text)

        # ---- 分支②：切不出两个句子的回退 ----
        # 不足两句（如一整段没有句号、或英文小数/缩写导致正则只切出 1 段）却又超了预算——
        # 按句装窗无从谈起，只能对整个 token 序列做硬滑窗：
        # 每窗 max_tokens，步长 max_tokens - overlap（即相邻窗重叠 overlap_tokens），
        # 纯 token 边界，会切在句子中间，是"保长度、牺牲句子完整性"的兜底
        if len(sentences) <= 1:
            return self._token_window(self._enc.encode(text))

        # ---- 分支③：正常路径——按句装窗 ----
        # _pack_sentences 逐句累加，凑满 max_tokens 封一窗；新窗以前 至 窗尾部约 overlap_tokens 的【完整句子】开头（保持跨窗语义连贯）；
        # 全程不切断句子——除非某一句本身就超过整个预算（ _pack_sentences 调用 _token_window）
        return self._pack_sentences(sentences)

    def split_doc(self, path: str, sections: list[Section], title: str = "") -> list[Chunk]:
        """文档级：逐章节遍历，把带章节结构的一篇文档切成 Chunk 列表。

        进切块前先过三道丢弃判据，被丢弃的章节不产生块、不占块序号：
        参考文献章节（_is_reference）、期刊样板章节（_is_boilerplate）、
        解析残渣正文（_is_fragment）。

        Args:
            path: 文档**绝对路径**（进块 id 与元数据；块 id 因此与文档在语料里的
                位置绑定——挪库或改语料根会让全部块 id 变化，只能全量重建）。
            sections: 章节列表（见 Section）。
            title: 文档标题；与章节标题一起拼成每个窗口的首行前缀。

        Returns:
            Chunk 列表，序号在文档内全局递增；无可用章节时为空列表。

        其余语义（幂等 id、先删后建配合）与原实现一致，见类注释。
        """
        chunks: list[Chunk] = []
        idx = 0  # 文档内全局块序号 id

        # 1. 遍历每个章节（标题, 正文, 位置）。
        for sec in sections:
            heading, text = sec.heading, sec.text
            # 2. 丢弃判据：参考文献 / 期刊样板（按标题）、解析残渣（按正文）。
            #    被丢弃的章节不产生块；后续章节的块序号照常顺延。
            if self._is_reference(heading) or self._is_boilerplate(heading):
                continue
            if self._is_fragment(text):
                continue
            # 3. 否则，对章节正文调用 `_split_long` 分割（可能返回一个或多个片段）。
            # 4. 逐窗生成块，**存干净正文**——「标题 > 章节」前缀由使用点现拼
            #    （见 indexed_text），不写进库：前缀是派生的展示视图，改它不必重写库。
            for part in self._split_long(text):
                # 5. 为每个片段生成一个 Chunk 对象，其中 id 由 `sha1(绝对路径 + 全局序号)[:16]` 生成。
                chunk_id = hashlib.sha1(f"{path}:{idx}".encode()).hexdigest()[:CHUNK_ID_LEN]
                chunks.append(Chunk(
                    id=chunk_id, text=part, path=path, title=title, heading=heading,
                    position=sec.positions, chunk_index=idx,
                ))
                # 6. 全局序号 `idx` 从 0 开始递增，保证同一文档内不同位置的块 ID 唯一且稳定。
                idx += 1
        return chunks
