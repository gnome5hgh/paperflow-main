"""RagIndexer：把语料库里的 PDF 索引进向量库与 BM25。

索引方式是增量扫描：用文件的修改时间判断哪些论文变了，只重索引新增或
变更的论文；被删除的论文则从索引里清理掉。索引根只有论文目录——笔记是 agent
自己的产物，短、可整篇读，不需要检索（「检索自己的笔记」这项能力是有意移除的）。

幂等设计：单篇文档的重新索引用"先删后建"。因为块 id 由路径加序号哈希
生成、与内容无关，文档内容收缩或删掉某些章节时，原来位置上的旧块必须
显式清除，否则会永远残留在索引里。BM25 是向量库文档在内存里的投影，
删除和写入必须与向量库成对执行才能保持一致。

解析来源单一：文本与结构由本地版面解析给出（见 ``paperflow.rag.parsers.pdf_extract``），
抽出的 markdown 再按 ``#`` 行分节；不依赖任何外部解析服务，因此不存在「解析器降级」
这种状态。

媒体块：PDF 的图与表各成一类块，接在章节块之后——表格单元格里的数值与图注里的
结论在章节正文中往往不出现，不独立成块就检索不到。区域定位由 ``vision`` 提供，
本模块只负责把它转成块（存注文与区域内文本，不落图）。

状态版本门控：状态文件记录「配方哈希」（见 ``_recipe_hash``）而非手写版本号。
所有决定「产出哪些块」的配置输入（切块 max/overlap、embed_model）连同
``RECIPE_LOGIC_REVISION`` 一起做 sha256；YAML 里改任一参数即指纹变化 →
下次启动放弃旧状态、全量重扫重嵌——否则旧配方的块会因文件 mtime 未变而永远
残留。纯算法逻辑改动无法被参数枚举，改 ``RECIPE_LOGIC_REVISION`` 手动 +1 兜底。

``docs`` 的取值形状保持 ``{绝对路径: mtime 浮点数}`` 不变——外部消费方按数值
比对 mtime，改成对象会静默破坏其增量跳过能力。
"""
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path

from paperflow.rag.constants import CHUNK_ID_LEN, CHUNK_TYPE_FIGURE, CHUNK_TYPE_TABLE
from paperflow.rag.domain import (
    Chunk,
    IndexOutcome,
    IndexRunOutcome,
    IndexStatus,
    Section,
    indexed_text,
)
from paperflow.rag.parsers.pdf_extract import extract_pdf

logger = logging.getLogger(__name__)

#: 配方哈希的逻辑版本号：切块/解析「算法逻辑」修订号（非参数）。参数
#: （``rag.chunker.*``、embed_model）自动进 ``_recipe_hash`` 指纹；算法逻辑改动
#: （如 ``_pack_sentences`` 改写、章节产出规则变更、丢弃判据词表调整）无法被参数
#: 枚举，只能手动 +1 → 下次 ``index_all`` 全量重扫重嵌。仅当切块/解析逻辑改动、
#: 产出块集合可能变化时才改，不要为参数调整而动它。
#: 修订 2：split_doc 新增期刊样板章节（致谢/资助/利益冲突/数据可用性等）与
#: 正文残渣（不成句微碎片）的丢弃判据，产出块集合变小。
#: 修订 3：PDF 解析从外部解析服务换成本地版面解析 + 统一按 ``#`` 行分节，
#: 章节划分与标题来源都变了，产出块集合与文本随之改变。
RECIPE_LOGIC_REVISION = 3


def _recipe_hash(cfg) -> str:
    """把「决定产出哪些块」的配置输入散列成状态版本指纹。

    输入三要素：
    - ``RECIPE_LOGIC_REVISION``：切块/解析算法逻辑的手动修订号（参数枚举不到的改动兜底）；
    - ``rag.chunker.max_tokens`` / ``overlap_tokens``：切块窗口参数；
    - ``rag.embedding.embed_model``：换模型（即便同维）旧向量也必须失效，否则新旧
      向量混在同一 Milvus 集合、检索质量静默下降。

    ``sort_keys=True`` 保证同输入稳定；``ensure_ascii=False`` 让中文模型名可读
    （不影响哈希值）。

    Args:
        cfg: PaperFlowConfig 实例。

    Returns:
        str: 64 位十六进制 sha256 指纹。
    """
    payload = json.dumps({
        "logic": RECIPE_LOGIC_REVISION,
        "chunk_max_tokens": cfg.rag.chunker.max_tokens,
        "chunk_overlap_tokens": cfg.rag.chunker.overlap_tokens,
        "embed_model": cfg.rag.embedding.embed_model,
    }, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _split_markdown(lines: list[tuple[str, tuple[int, int, int, int, int]]]
                    ) -> tuple[str, list[Section]]:
    """按 ``#`` 行把正文切成章节，顺带取首个一级标题作文档标题。

    Args:
        lines: ``(行文本, 该行的位置)`` 列表。位置为
            ``(页, left, right, top, bottom)``，用于给检索块标注页码与坐标。

    Returns:
        tuple[str, list[Section]]：(文档标题, 章节列表)。标题只认 ``# `` 开头的
        一级标题且取第一个，``##`` 及更深的不算；取不到时为空串。每个章节的位置
        是它覆盖到的行的位置去重后的集合。
    """
    title = ""
    sections: list[Section] = []
    cur_head, cur_body = "", []
    cur_pos: list[tuple[int, int, int, int, int]] = []

    def flush() -> None:
        """把攒着的当前章节收进 sections（位置去重且保持出现顺序）。"""
        if cur_head or cur_body:
            sections.append(Section(cur_head, "\n".join(cur_body),
                                    tuple(dict.fromkeys(cur_pos))))

    for text, pos in lines:
        if text.startswith("#"):
            flush()
            cur_head, cur_body, cur_pos = text.lstrip("# "), [], []   # 去掉 # 和后面的空格
            if not title and text.startswith("# "):
                title = text[2:].strip()
        else:
            cur_body.append(text)
        cur_pos.append(pos)
    flush()
    return title, sections


@dataclass
class _FileContent:
    """单篇文档解析产物：切块所需的全部原料。

    Attributes:
        title: str，文档标题（版面/元数据标题；取不到为空串）
        sections: list[Section]，章节列表（带各自的版面位置）
    """
    title: str
    sections: list[Section]


class RagIndexer:
    """索引器：维护"文档路径 → 修改时间"的状态文件，据此做增量索引。

    职责：
    - 将语料目录里的 PDF 切块、编码后写入向量库和 BM25。
    - 通过修改时间戳判断文档是否变更，只增量更新有变化的文档。
    - 自动清理已被删除的文档的索引数据。
    - 维护索引状态文件（rag/index_state.json，带配方哈希版本），保证跨进程的增量一致性；
      配方哈希不符时放弃旧状态走全量重扫（切块参数/逻辑升级后的自愈机制）。

    Attributes:
        service: RAGService 单例，与检索器共享底层组件
        _state_path: Path，索引状态文件路径（workspace/rag/index_state.json，带配方哈希）
    """

    def __init__(self, service):
        """绑定门面服务，并定位索引状态文件（工作区 rag/ 下的 index_state.json）。

        Args:
            service: RAGService 单例，索引器和检索器共享 service 中的组件（向量库、BM25、编码器等）。
        """
        self.service = service
        # 状态文件：记录已索引文档的绝对路径 → 最后修改时间（浮点数时间戳）
        self._state_path = Path(service.config.runtime.workspace) / "rag" / "index_state.json"

    @property
    def _recipe(self) -> str:
        """当前配置的配方指纹——现算不缓存。

        原为 __init__ 快照，构造后 config 被改会得到陈旧指纹、使状态版本比对
        失效；指纹只是 5 个标量的 sha256，现算成本可忽略。

        Returns:
            64 位十六进制 sha256 指纹。
        """
        return _recipe_hash(self.service.config)

    # ---------- 路径/状态工具 ----------
    def _rel_path(self, path: str) -> str | None:
        """判断路径是否属于语料，并给出它相对语料根的路径。

        现在的用途**只是「这个文件属不属于语料」这道判定**——存储与块 id 一律用
        绝对路径（见 Chunk.path），调用方拿它当真假用：返回 None 即跳过。
        返回相对路径而非布尔值是为了在报错文案与体检清单里给出更短的展示路径。

        不在论文目录下的路径一律返回 None——不能用文件名代替：别的目录（记忆、
        笔记）里的文件也会触发索引钩子，用文件名会把它们的同名文件当成同一篇，
        后索引的覆盖并删掉前者的块。

        Args:
            path: 绝对或相对路径（会被解析为绝对路径）。

        Returns:
            str | None: 相对论文目录的路径，若文件不在语料根下则返回 None。
        """
        abs_path = Path(path).resolve()
        try:
            return str(abs_path.relative_to(
                Path(self.service.config.corpus.pdf_dir).resolve()))
        except ValueError:
            return None


    def _read_state(self) -> tuple[object, dict] | None:
        """读原始状态文件，返回 (版本号, docs)。

        返回值不做版本判断——版本门控由调用方决定（增量更新要求同版本，
        index_all 遇到不符版本则全量重扫）。版本号即配方哈希（字符串）；
        旧格式裸 dict 按版本 0 处理。

        Returns:
            tuple[object, dict] | None: (version, {绝对路径: mtime})；
            文件不存在或 JSON 非法返回 None。
        """
        if not self._state_path.exists():
            return None
        try:
            raw = json.loads(self._state_path.read_text())
        except (json.JSONDecodeError, OSError):
            # JSON 损坏等同于状态缺失：调用方走全量重扫，绝不让坏文件炸掉索引
            return None
        if isinstance(raw, dict) and isinstance(raw.get("docs"), dict):
            return raw.get("version", 0), dict(raw["docs"])
        # 旧格式（裸 {abs_path: mtime}）按版本 0 处理
        return 0, dict(raw) if isinstance(raw, dict) else {}

    def _save_state(self, state: dict) -> None:
        """把状态按当前配方哈希格式写入（自动创建父目录）。

        Args:
            state: {绝对路径: mtime} 映射；版本号（配方哈希）由本方法统一补上，
                   调用方只管 docs 内容。**取值形状必须是 float**——外部消费方
                   按数值比对 mtime，改成对象会使其永远
                   判定「已变更」而静默丢失增量能力。
        """
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps({"version": self._recipe, "docs": state}))

    # ---------- 文档解析 → 分块 ----------
    def _parse_file(self, path: Path) -> _FileContent:
        """解析一篇 PDF，产出 chunker 切块所需的全部原料。

        文本与章节由本地版面解析还原成 markdown，再按同一套「按 ``#`` 行分节」逻辑
        切章节。文档标题取自版面/元数据，绝不用文件名充当标题。

        Args:
            path: PDF 路径。

        Returns:
            _FileContent: 解析产物（title/sections）。
        """
        extracted = extract_pdf(str(path))
        # 每个渲染块的位置随它拆出的每一行重复出现，分节后即成为该章节的
        # 位置集合（一个块内的行同属一段，位置相同是准确的）。
        lines: list[tuple[str, tuple[int, int, int, int, int]]] = []
        for blk in extracted.blocks:
            pos = (blk.page, blk.left, blk.right, blk.top, blk.bottom)
            lines.extend((ln, pos) for ln in blk.text.splitlines())
        _, sections = _split_markdown(lines)
        # PDF 的正文里不出现一级标题（级别从二级起），标题只能来自版面/元数据
        return _FileContent(extracted.title or "", sections)

    def _media_chunks(self, path: str, start_index: int) -> list[Chunk]:
        """把一篇 PDF 的图/表区域转成检索块，序号接在章节块之后。

        每块存两样东西：**注文进 `caption`、区域内文字进 `text`**。检索结果的
        「章节」列取 `heading or caption`，所以媒体块那一列显示的就是表注/图注原文；
        摘录给出的则是区域内的文字。两者都为空的区域不产块——没有可检索内容的块
        只会占位。注文偏长时进前缀会加长编码输入，但正文与窗口不受影响。

        不写图片、不入库图像：块只承载文字，图本身另有 analyze_figures 工具按需看。

        Args:
            path: 文档绝对路径（进块 id 与元数据）。
            start_index: 起始块序号（章节块数量），保证 id 空间不重叠。

        Returns:
            list[Chunk]: 媒体块（图与表，可能为空）。
        """
        try:
            figures = self.service.extract_figures(path)
        except Exception as e:
            # 图表定位是加分项：失败只丢媒体块，章节块照常入库
            logger.warning("图表区域定位失败，本篇不产媒体块：%s", e)
            return []

        # 惰性 import：vision 有自己的重依赖，不在包导入期拉起
        from paperflow.vision.constants import FigureType

        chunks: list[Chunk] = []
        idx = start_index
        for f in figures:
            caption = " ".join((f.caption or "").split())
            body = " ".join((f.image_text or "").split())
            if not caption and not body:
                continue
            ctype = (CHUNK_TYPE_TABLE if f.fig_type == FigureType.Table
                     else CHUNK_TYPE_FIGURE)
            bounds = f.region_boundary
            position = ((f.page + 1, int(bounds.x1), int(bounds.x2),
                         int(bounds.y1), int(bounds.y2)),) if bounds else ()
            chunk_id = hashlib.sha1(f"{path}:{idx}".encode()).hexdigest()[:CHUNK_ID_LEN]
            chunks.append(Chunk(
                id=chunk_id, text=body, path=path, heading="", caption=caption,
                chunk_type=ctype, position=position, chunk_index=idx,
            ))
            idx += 1
        return chunks

    def _embed_chunks(self, chunks: list[Chunk]):
        """把一批块编码成向量（供写入向量库）。

        编码用的是带前缀的文本（`indexed_text`），与 BM25 侧的输入同源——库里存的
        则是干净正文。

        Args:
            chunks: Chunk 对象列表。

        Returns:
            np.ndarray: 向量矩阵，形状为 (len(chunks), dim)。
        """
        embedder = self.service._ensure_embedder()
        vecs = embedder([indexed_text(c) for c in chunks])
        return vecs

    def _derive_state_from_store(self, store) -> dict:
        """从向量库的元数据重建索引状态（当状态文件丢失时使用）。

        向量库里每个块都带其所属文档的**绝对路径**与修改时间；同一文档的多个块
        共享同一修改时间，因此取最大值即可。

        Args:
            store: VectorStore 实例。

        Returns:
            dict: {绝对路径: 修改时间戳}，用于后续增量比对。
        """
        state: dict = {}
        for chunk, mtime in store.all_documents():
            # 同一文档可能有多块，取最新的 mtime
            if chunk.path not in state or mtime > state[chunk.path]:
                state[chunk.path] = mtime
        return state

    # ---------- 公开 API ----------
    def index_document(self, path: str) -> IndexOutcome:
        """单篇文档的增量重索引入口，文档写入/编辑/下载完成后调用。

        为什么必须"先删后建"？
        - 块 ID 由绝对路径和块序号哈希生成，与内容无关。
        - 若文档内容缩短（如删掉某些章节），旧块 ID 不再出现，若不清除则会永久残留。
        - 因此每次索引必须先清除旧块，再写入新块。

        边界条件处理：
        - 若文件不存在，返回 skipped（不索引）。
        - 若文件不在知识库根目录（如记忆目录），返回 skipped（避免跨目录同名文件冲突）。
        - 若文档切块后为空（如全空白或只有参考文献），删除旧块后返回 empty，不写入新块、状态文件不更新。
        - 若文档内容未变（修改时间未变），外部调用方应避免调用本方法（但本方法本身不检查）。

        状态版本门控：仅当状态文件缺失或配方哈希与当前一致时才写入状态。配方
        不符说明存在旧配方状态、待 index_all 全量重扫（旧状态里还留着其他文档的
        记录，是删除清理的依据），此时覆盖写单篇状态会抹掉这份依据（半更新），
        让下次全量重扫的清理环节失效。

        Args:
            path: 文档的绝对路径（或相对路径，会被解析）。

        Returns:
            IndexOutcome，本次入库的状态、写入块数与原因。状态为 "indexed"
            （写入 chunks 块）、"skipped"（文件不存在或不在语料根下，reason 说明）、
            "empty"（旧块已清理但切不出新块，reason 说明）。reason 供调用方如实转述。
        """
        p = Path(path)
        if not p.exists():
            return IndexOutcome("skipped", reason="文件不存在")   # 索引不存在的文件：跳过

        key = str(p.resolve())
        if self._rel_path(key) is None:
            # 非语料目录的路径（笔记、记忆）直接跳过。
            # 那些目录里的写入也会触发本钩子，但本模块只索引 PDF；
            # 若不跳过，同名文件会被当成同一篇，导致先入索引的块被静默覆盖删除。
            return IndexOutcome("skipped", reason="不在语料根目录下（只索引 PDF）")

        # 1. 解析文档，获得切块原料（章节 + 标题）
        parsed = self._parse_file(p)
        store = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # 2. 清除该文档的旧索引（定点查询替代全表扫描）。存储键与块 id 都用绝对路径。
        old_ids = store.doc_chunk_ids(key)
        for did in old_ids:
            bm25.remove_document(did)
        store.delete_doc(key)

        # 3. 切分：章节块 + 图/表媒体块，过滤空白块
        chunks = self.service.chunker.split_doc(key, parsed.sections, title=parsed.title)
        if p.suffix.lower() == ".pdf":
            chunks.extend(self._media_chunks(key, start_index=len(chunks)))
        chunks = [c for c in chunks if c.text.strip() or c.caption.strip()]
        if not chunks:
            # 文档被清空：旧块已删，无需写新内容
            return IndexOutcome("empty", reason="切块后无内容（旧块已清理）")

        # 4. 编码并写入
        vecs = self._embed_chunks(chunks)
        mtime = p.stat().st_mtime
        # 向量数据库存：① 块正文；② 正文（带前缀）压缩成的一个 1024 维浮点向量；③ 元数据
        store.upsert(chunks, vecs, mtime=mtime)
        # bm25存的是：① 带前缀文本分词后的 token 列表，比如：["多意图","执行","clarification","触发","判定",...]；② 词频矩阵 + idf 表（“这个词在几篇文档里出现过”的统计）
        bm25.add_documents([(c.id, indexed_text(c)) for c in chunks])

        # 5. 更新状态文件——仅「状态缺失或同版本」时写入。状态缺失时没有可
        #    丢失的清理依据，正常写入让热更新路径的状态记录照常生效；版本
        #    不符时旧状态是 index_all 删除清理的依据，覆盖写会造成半更新
        #    （详见 docstring 的状态版本门控说明）。
        raw = self._read_state()
        if raw is None or raw[0] == self._recipe:
            docs = raw[1] if raw else {}
            docs[key] = mtime
            self._save_state(docs)

        return IndexOutcome("indexed", chunks=len(chunks))

    def index_all(self) -> IndexRunOutcome:
        """全量增量扫描：只重索引新增或变更的文档，并清理已删除的文档。

        这是 `index_document` 的批量版本，适用于启动时或定时任务。

        状态版本门控：
        - 状态文件缺失、JSON 损坏或配方哈希与当前不符（切块参数/逻辑
          升级后的首次运行）→ 放弃旧状态，全量重扫重嵌。
        - 不从向量库元数据恢复不符配方的状态：库内块无法确认由当前配方
          产出，恢复会让旧配方块因 mtime 未变而永久残留。

        一致性兜底机制（仅同版本状态下生效，保留原有两兜底）：
        - 若向量库为空但状态文件非空 → 状态文件过期（可能手动清空过向量库），清空状态。
        - 若状态文件为空但向量库非空 → 状态文件丢失，从向量库元数据重建状态。

        BM25 重建策略：
        - 每次全量扫描都会从向量库的全部文档重建 BM25，因为 BM25 仅驻留内存，进程重启后为空，必须依赖向量库作为可信数据源。

        删除清理策略：
        - 已删除的文档逐个移除（按路径定点查块 id，替代全表扫描），无需整库重建，避免开销。
        - 若遇到状态中的路径不在论文目录下（旧版本残留），则跳过清理（防御性）。

        增量索引策略：
        - 对于变更的文档，直接调用 `index_document`（内部会先删后建），保证每个文档的一致性。

        Returns:
            IndexRunOutcome，本次扫描的统计：重索引文档数（changed）、清理文档数
            （removed）、写入块数合计（chunks）、重建后 BM25 文档数（bm25_docs）
            与是否走了全量重扫（recipe_reset，状态缺失或配方哈希不符时为 True）。
        """
        store = self.service._ensure_vector_store()
        raw = self._read_state()

        if raw is None or raw[0] != self._recipe:
            # 状态缺失或配方哈希不符（参数/逻辑升级后的首次运行）：放弃旧状态，
            # 全量重扫重嵌。不能从向量库元数据恢复——库内块无法确认由当前配方
            # 产出，恢复会让旧配方块因 mtime 未变而永久残留。
            recipe_reset = True
            state = {}
        else:
            recipe_reset = False
            state = raw[1]
            # 同版本下保留原有两兜底：向量库被清空 → 状态作废；状态空 → 从元数据恢复。
            if store.count() == 0 and state:
                state = {}
            elif not state and store.count() > 0:
                state = self._derive_state_from_store(store)

        # 2. 从向量库的全部文档整体重建 BM25（因为 BM25 是内存索引，进程重启后为空）。
        self.service._ensure_bm25().rebuild(
            [(c.id, indexed_text(c)) for c, _mtime in store.all_documents()])

        # 收集待索引论文：扫描论文目录，按修改时间比对找出变更项。
        roots = [Path(self.service.config.corpus.pdf_dir)]
        new_state: dict = {}
        changed: list[Path] = []
        seen: set[Path] = set()

        # 3. 扫描论文目录，列出所有 .pdf 文件。
        for root in roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not p.is_file() or p.suffix.lower() != ".pdf":
                    continue
                seen.add(p.resolve())
                key = str(p.resolve())

                # 4. 对比状态文件中的修改时间，筛选出变更的文件。
                mtime = p.stat().st_mtime
                if state.get(key) != mtime:
                    changed.append(p)
                new_state[key] = mtime

        # 5. 从状态中找出已删除的文件（状态里有记录但本次扫描没见到的）。
        removed = [k for k in state if k not in {str(s) for s in seen}]

        # 6. 清理已删除的文档。
        removed_count = 0
        for abs_path in removed:
            # 防御性跳过：正常流程下状态文件里的键只可能是论文目录下的路径
            # （写入前就用 _rel_path 过滤过），但旧版本可能残留其他目录的键——
            # 遇到时跳过，不要误删对应块。
            if self._rel_path(abs_path) is None:
                continue
            # 定点查询该文档的所有块 ID（替代全表扫描后按路径过滤）
            rm_ids = store.doc_chunk_ids(abs_path)
            for did in rm_ids:
                self.service._ensure_bm25().remove_document(did)
            store.delete_doc(abs_path)
            removed_count += 1

        # 7. 增量索引变更的文档，累计本次写入的块数。
        total_chunks = 0
        for p in changed:
            outcome = self.index_document(str(p))
            total_chunks += outcome.chunks

        # 8. 保存新的状态文件（_save_state 自动带当前配方哈希）。
        self._save_state(new_state)

        # bm25_docs 取扫描结束后的实际条数：重建在扫描前用旧库内容完成，
        # 变更文档是重建之后才增量写入 BM25 的，故不能拿重建输入的长度充当
        # （首次扫描时旧库为空，那样会恒为 0）。
        return IndexRunOutcome(changed=len(changed), removed=removed_count,
                               chunks=total_chunks,
                               bm25_docs=self.service._ensure_bm25().count(),
                               recipe_reset=recipe_reset)

    def status(self) -> IndexStatus:
        """体检：把状态文件、向量库、关键词索引与语料根对照一遍（只读）。

        回答「库里现在到底有什么、和语料是否一致」，不写任何东西、也不触发重扫——
        发现 `ghost` 或 `not_indexed` 时要不要收敛由调用方决定（跑一次 index_all）。

        Returns:
            IndexStatus 快照。Milvus 不可达时库侧三个计数留 0（避免为探测白等 RPC 超时），
            其余数值仍按状态文件与磁盘算出。
        """
        st = IndexStatus(recipe=self._recipe, milvus_ok=self.service.milvus_available())

        raw = self._read_state()
        docs: dict = {}
        if raw is not None:
            version, docs = raw
            st.state_present = True
            st.state_version = version
            st.recipe_in_sync = version == st.recipe
            st.indexed_docs = len(docs)

        # 论文目录扫描：磁盘有而状态没有 = 新增未入库；状态有而磁盘没有 = 删除未收敛。
        # 同一套 .pdf 过滤与 _rel_path 围栏，保证口径与索引扫描一致。
        # 清单用相对路径展示，便于人读；状态文件里的键是绝对路径。
        corpus: set[str] = set()
        root = Path(self.service.config.corpus.pdf_dir)
        if root.exists():
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() == ".pdf":
                    corpus.add(str(p.resolve()))
        st.corpus_docs = len(corpus)
        known = set(docs)
        st.not_indexed = [r for r in (self._rel_path(p) for p in sorted(corpus - known)) if r]
        st.ghost = [r for r in (self._rel_path(p) for p in sorted(known - corpus)) if r]

        if st.milvus_ok:
            store = self.service._ensure_vector_store()
            st.store_chunks = store.count()
            # all_documents() 的每项是 (块, mtime)，文档身份取块路径
            st.store_docs = len({c.path for c, _ in store.all_documents()})
            st.bm25_docs = self.service._ensure_bm25().count()
        return st
