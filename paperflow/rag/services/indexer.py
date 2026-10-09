"""RagIndexer：把知识库里的文档（Markdown 笔记和 PDF）索引进向量库与 BM25。

索引方式是增量扫描：用文件的修改时间判断哪些文档变了，只重索引新增或
变更的文档；被删除的文档则从索引里清理掉。

幂等设计：单篇文档的重新索引用"先删后建"。因为块 id 由路径加序号哈希
生成、与内容无关，文档内容收缩或删掉某些章节时，原来位置上的旧块必须
显式清除，否则会永远残留在索引里。BM25 是向量库文档在内存里的投影，
删除和写入必须与向量库成对执行才能保持一致。

解析来源单一：PDF 与笔记都走「抽取出文本 → 按 ``#`` 行分节」这一条路，PDF 的
文本与结构由本地版面解析给出（见 ``paperflow.rag.parsers.pdf_extract``），不依赖
任何外部解析服务，因此不存在「解析器降级」这种状态。

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
from dataclasses import dataclass, field
from pathlib import Path

from paperflow.rag.parsers.chunker import Chunk, Section
from paperflow.rag.parsers.pdf_extract import extract_pdf

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


def _split_markdown(lines: list[tuple[str, tuple[int, int, int, int, int] | None]]
                    ) -> tuple[str, list[Section]]:
    """按 ``#`` 行把正文切成章节，顺带取首个一级标题作文档标题。

    PDF（本地版面解析还原出的 markdown）与笔记文件走同一条路，因此这里不分来源；
    两者唯一的差别是每行能否带上版面位置——PDF 有（用于给检索块标注页码与坐标），
    笔记没有。

    Args:
        lines: ``(行文本, 该行的位置或 None)`` 列表。位置为
            ``(页, left, right, top, bottom)``。

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
        if pos is not None:
            cur_pos.append(pos)
    flush()
    return title, sections


def _markdown_lines(text: str) -> list[tuple[str, None]]:
    """把一段纯文本按行包装成分节输入（位置一律为 None）。"""
    return [(ln, None) for ln in text.splitlines()]


@dataclass
class _FileContent:
    """单篇文档解析产物：切块所需的全部原料。

    Attributes:
        title: str，文档标题（PDF=版面/元数据标题，笔记=首个 H1；取不到为空串）
        sections: list[Section]，章节列表（带各自的版面位置；笔记无位置）
    """
    title: str
    sections: list[Section]


@dataclass
class IndexOutcome:
    """单篇文档的入库结果。

    Attributes:
        status: str，"indexed"（已入库）/ "skipped"（未入库：文件不存在或不在语料根下）
            / "empty"（旧块已清理但新内容切不出块）
        chunks: int，本次写入的块数（仅 "indexed" 有意义）
        reason: str | None，跳过或空结果的原因，供调用方如实转述
    """
    status: str
    chunks: int = 0
    reason: str | None = None


@dataclass
class IndexRunOutcome:
    """全量扫描的结果统计。

    Attributes:
        changed: int，本次重新索引的文档数
        removed: int，本次清理的已删除文档数
        chunks: int，本次写入的块数合计
        bm25_docs: int，重建后 BM25 中的文档数
        recipe_reset: bool，是否因配方哈希不符（或状态缺失）放弃旧状态走了全量重扫
    """
    changed: int = 0
    removed: int = 0
    chunks: int = 0
    bm25_docs: int = 0
    recipe_reset: bool = False


@dataclass
class IndexStatus:
    """索引体检快照（只读，不触发任何写入）。

    Attributes:
        state_present: bool，状态文件是否存在且可解析
        state_version: object，状态文件里记的配方哈希（None = 无状态）
        recipe: str，当前配置算出的配方哈希
        recipe_in_sync: bool，状态版本是否与当前配方一致（不一致 → 下次收敛会全量重扫）
        indexed_docs: int，状态文件记录的文档数
        store_chunks: int，向量库中的块数
        store_docs: int，向量库涉及的文档数（去重）
        bm25_docs: int，内存关键词索引里的文档数
        ghost: list[str]，状态有记录但磁盘已不存在的文档（相对路径）——删除未收敛的残留
        not_indexed: list[str]，语料根下有文件但状态里没有（相对路径）——新增未入库
        corpus_docs: int，语料根下扫描到的 .md / .pdf 文件数
        milvus_ok: bool，Milvus 可连性（探测带 TTL）
    """

    state_present: bool = False
    state_version: object = None
    recipe: str = ""
    recipe_in_sync: bool = False
    indexed_docs: int = 0
    store_chunks: int = 0
    store_docs: int = 0
    bm25_docs: int = 0
    ghost: list = field(default_factory=list)
    not_indexed: list = field(default_factory=list)
    corpus_docs: int = 0
    milvus_ok: bool = False


class RagIndexer:
    """索引器：维护"文档路径 → 修改时间"的状态文件，据此做增量索引。

    职责：
    - 将工作区内的 Markdown 笔记和 PDF 切块、编码后写入向量库和 BM25。
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

        如果路径既不在笔记目录也不在 PDF 目录下，返回 None——不能用文件名代替。
        原因：还有其他目录（如记忆目录）里的文件也会触发索引钩子，若用文件名
        代替，与笔记目录里同名的文件（如 memory/shared.md 与 note/shared.md）
        会被当成同一篇，导致后索引的文档静默覆盖、删除前者的块，造成数据丢失。

        Args:
            path: 绝对或相对路径（会被解析为绝对路径）。

        Returns:
            str | None: 相对路径（如 "note/paper.md"），若文件不在语料根下则返回 None。
        """
        abs_path = Path(path).resolve()
        # 依次尝试在笔记目录和 PDF 目录下计算相对路径
        for root in (Path(self.service.config.corpus.note_dir).resolve(),
                     Path(self.service.config.corpus.pdf_dir).resolve()):
            try:
                return str(abs_path.relative_to(root))
            except ValueError:
                continue
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
        """读取并解析文档，产出 chunker 切块所需的全部原料。

        两条来源共用同一套分节逻辑（按 ``#`` 行切章节）：PDF 的文本与章节由本地
        版面解析还原成 markdown 后再切，笔记文件本身已是 markdown。文档标题都取自
        内容（PDF 取版面/元数据标题、笔记取首个一级标题），绝不用文件名充当标题。

        Args:
            path: 文档路径。

        Returns:
            _FileContent: 解析产物（title/sections）。
        """
        if path.suffix.lower() == ".pdf":
            extracted = extract_pdf(str(path))
            # 每个渲染块的位置随它拆出的每一行重复出现，分节后即成为该章节的
            # 位置集合（一个块内的行同属一段，位置相同是准确的）。
            lines: list[tuple[str, tuple[int, int, int, int, int] | None]] = []
            for blk in extracted.blocks:
                pos = (blk.page, blk.left, blk.right, blk.top, blk.bottom)
                lines.extend((ln, pos) for ln in blk.text.splitlines())
            _, sections = _split_markdown(lines)
            # PDF 的正文里不出现一级标题（级别从二级起），标题只能来自版面/元数据
            return _FileContent(extracted.title or "", sections)

        text = path.read_text(encoding="utf-8")
        title, sections = _split_markdown(_markdown_lines(text))
        return _FileContent(title, sections)

    def _embed_chunks(self, chunks: list[Chunk]):
        """把一批块文本编码成向量（供写入向量库）。

        Args:
            chunks: Chunk 对象列表。

        Returns:
            np.ndarray: 向量矩阵，形状为 (len(chunks), dim)。
        """
        embedder = self.service._ensure_embedder()
        vecs = embedder([c.text for c in chunks])
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
        for _id, _doc, path, mtime in store.all_documents():
            # 同一文档可能有多块，取最新的 mtime
            if path not in state or mtime > state[path]:
                state[path] = mtime
        return state

    # ---------- 公开 API ----------
    def index_document(self, path: str) -> IndexOutcome:
        """单篇文档的增量重索引入口，文档写入/编辑/下载完成后调用。

        为什么必须"先删后建"？
        - 块 ID 由相对路径和块序号哈希生成，与内容无关。
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
            # 非知识库根目录的路径（如记忆目录）直接跳过，避免与同名笔记冲突。
            # 记忆文件的写入也会触发本钩子，但本模块只索引笔记和 PDF；
            # 若不跳过，记忆目录下与笔记目录同名的文件会被当成同一篇，导致笔记的块被静默覆盖删除。
            return IndexOutcome("skipped", reason="不在语料根目录下（只索引笔记与 PDF）")

        # 1. 解析文档，获得切块原料（章节 + 标题）
        parsed = self._parse_file(p)
        store = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # 2. 清除该文档的旧索引（定点查询替代全表扫描）。存储键与块 id 都用绝对路径。
        old_ids = store.doc_chunk_ids(key)
        for did in old_ids:
            bm25.remove_document(did)
        store.delete_doc(key)

        # 3. 切分章节，过滤空白块
        chunks = self.service.chunker.split_doc(key, parsed.sections, title=parsed.title)
        chunks = [c for c in chunks if c.text.strip()]   # 过滤空白文本的块，避免产生无意义向量
        if not chunks:
            # 文档被清空：旧块已删，无需写新内容
            return IndexOutcome("empty", reason="切块后无内容（旧块已清理）")

        # 4. 编码并写入
        vecs = self._embed_chunks(chunks)
        mtime = p.stat().st_mtime
        # 向量数据库存：① 块正文；② 正文压缩成的一个 1024 维浮点向量；③ 元数据
        store.upsert(chunks, vecs, mtime=mtime)
        # bm25存的是：① 正文分词后的 token 列表，比如：["多意图","执行","clarification","触发","判定",...]；② 词频矩阵 + idf 表（“这个词在几篇文档里出现过”的统计）
        bm25.add_documents([(c.id, c.text) for c in chunks])

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
        - 若遇到状态中的路径不在笔记/PDF 目录下（旧版本残留），则跳过清理（防御性）。

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
        all_docs = list(store.all_documents())
        self.service._ensure_bm25().rebuild([(d[0], d[1]) for d in all_docs])

        # 收集待索引文档：扫描两个知识库根目录，按修改时间比对找出变更项。
        roots = [Path(self.service.config.corpus.note_dir),
                 Path(self.service.config.corpus.pdf_dir)]
        new_state: dict = {}
        changed: list[Path] = []
        seen: set[Path] = set()

        # 3. 扫描两个知识库目录，列出所有.md 和.pdf 文件。
        for root in roots:
            if not root.exists():
                continue
            # 递归遍历所有 .md 和 .pdf 文件
            for p in root.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in (".md", ".pdf"):
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
            # 防御性跳过：正常流程下状态文件里的键只可能是笔记/PDF 目录的路径
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

        # 语料根扫描：磁盘有而状态没有 = 新增未入库；状态有而磁盘没有 = 删除未收敛。
        # 同一套 .md/.pdf 过滤与 _rel_path 围栏，保证口径与索引扫描一致。
        # 清单用相对路径展示，便于人读；状态文件里的键是绝对路径。
        corpus: set[str] = set()
        for root in (Path(self.service.config.corpus.note_dir),
                     Path(self.service.config.corpus.pdf_dir)):
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in (".md", ".pdf"):
                    corpus.add(str(p.resolve()))
        st.corpus_docs = len(corpus)
        known = set(docs)
        st.not_indexed = [r for r in (self._rel_path(p) for p in sorted(corpus - known)) if r]
        st.ghost = [r for r in (self._rel_path(p) for p in sorted(known - corpus)) if r]

        if st.milvus_ok:
            store = self.service._ensure_vector_store()
            st.store_chunks = store.count()
            # all_documents() 的每项是 (id, text, path, mtime)，文档身份取 path
            st.store_docs = len({d[2] for d in store.all_documents()})
            st.bm25_docs = self.service._ensure_bm25().count()
        return st
