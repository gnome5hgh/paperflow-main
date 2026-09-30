"""RagIndexer：把知识库里的文档（Markdown 笔记和 PDF）索引进向量库与 BM25。

索引方式是增量扫描：用文件的修改时间判断哪些文档变了，只重索引新增或
变更的文档；被删除的文档则从索引里清理掉。

幂等设计：单篇文档的重新索引用"先删后建"。因为块 id 由路径加序号哈希
生成、与内容无关，文档内容收缩或删掉某些章节时，原来位置上的旧块必须
显式清除，否则会永远残留在索引里。BM25 是向量库文档在内存里的投影，
删除和写入必须与向量库成对执行才能保持一致。

切块产物：章节块由切块器切出（带「标题 > 章节」前缀首行），表格与图注
由本模块转成独立块接在章节块之后——GROBID 解析出的表格式数据（如基准
评测数字）在章节正文中不出现，不独立成块就永远检索不到。

状态版本门控：切块逻辑（前缀规则/句界/媒体块）变化时递增 _STATE_VERSION。
状态文件版本不符时放弃旧状态、全量重扫重嵌——否则旧配方切出的块会因
文件 mtime 未变而永远残留（对应 P6）。
"""
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from paperflow.rag.parsers.chunker import Chunk, context_prefix

#: 索引状态版本号：切块逻辑（前缀/句界/媒体块）变化时递增。加载到低版本
#: 状态时放弃旧状态、全量重扫重嵌，否则旧配方的块会因 mtime 未变而永远残留。
_STATE_VERSION = 2

#: 表格块文本上限（字符）：Milvus text 字段上限 65535，超长表格截断防御
_TABLE_TEXT_LIMIT = 8000


@dataclass
class _FileContent:
    """单篇文档解析产物：切块所需的全部原料。

    source: "pdf" | "note"；title: 文档标题（PDF=GROBID 主标题，笔记=H1，
    取不到为空串）；tables/figures: GROBID 提取的表格文本与图注（笔记与
    PyMuPDF 回退路径为空）。
    """
    source: str
    title: str
    sections: list[tuple[str, str]]
    tables: list[str]
    figures: list[str]


class RagIndexer:
    """索引器：维护"文档路径 → 修改时间"的状态文件，据此做增量索引。

    职责：
    - 将工作区内的 Markdown 笔记和 PDF 切块、编码后写入向量库和 BM25。
    - 通过修改时间戳判断文档是否变更，只增量更新有变化的文档。
    - 自动清理已被删除的文档的索引数据。
    - 维护索引状态文件（index_state.json，带版本号），保证跨进程的增量一致性；
      版本不符时放弃旧状态走全量重扫（切块逻辑升级后的自愈机制）。
    """

    def __init__(self, service):
        """绑定门面服务，并定位索引状态文件（工作区下的 index_state.json）。

        Args:
            service: RAGService 单例，索引器和检索器共享 service 中的组件（向量库、BM25、编码器等）。
        """
        self.service = service
        # 状态文件：记录已索引文档的绝对路径 → 最后修改时间（浮点数时间戳）
        self._state_path = Path(service.config.workspace) / "index_state.json"

    # ---------- 路径/状态工具 ----------
    def _rel_path(self, path: str) -> str | None:
        """把绝对路径转成相对知识库根目录的路径（用于文档 id 与元数据，跨机器稳定）。

        如果路径既不在笔记目录也不在 PDF 目录下，返回 None——不能用文件名代替。
        原因：还有其他目录（如记忆目录）里的文件也会触发索引钩子，若用文件名
        代替，与笔记目录里同名的文件（如 memory/shared.md 与 note/shared.md）
        会得到相同的相对路径和块 id，导致后索引的文档静默覆盖、删除前者的块，
        造成数据丢失。本模块只索引笔记和 PDF，非这两个目录的路径必须返回
        None，由调用方跳过处理。

        重要边界条件：
        - 路径必须位于 `note_dir` 或 `pdf_dir` 之下，否则返回 None。
        - 返回 None 时，调用方应跳过该文件，不进行索引（如记忆目录下的文件）。
        - 不能使用文件名代替相对路径，因为不同目录下的同名文件会导致块 ID 冲突。

        Args:
            path: 绝对或相对路径（会被解析为绝对路径）。

        Returns:
            str | None: 相对路径（如 "note/paper.md"），若文件不在知识库根目录下则返回 None。
        """
        abs_path = Path(path).resolve()
        # 依次尝试在笔记目录和 PDF 目录下计算相对路径
        for root in (Path(self.service.config.note_dir).resolve(),
                     Path(self.service.config.pdf_dir).resolve()):
            try:
                return str(abs_path.relative_to(root))
            except ValueError:
                continue
        return None

    def _rel_to_abs(self, rel: str) -> str:
        """把相对知识库根目录的路径还原成绝对路径（从向量库元数据重建索引状态时用）。

        适用场景：状态文件丢失，但向量库中仍存有文档元数据（包括相对路径）。
        本方法尝试在笔记目录和 PDF 目录下查找该相对路径对应的实际文件。

        边界情况：
        - 若文件已不存在（被删除），则无法确定其原属根目录，默认返回笔记目录下的路径。
        - 返回的路径可能指向一个不存在的文件，但在删除清理过程中，该路径不会出现在扫描结果中，后续逻辑会从状态中移除它。

        Args:
            rel: 相对路径（如 "note/paper.md"）。

        Returns:
            str: 解析后的绝对路径字符串。
        """
        for root in (Path(self.service.config.note_dir),
                     Path(self.service.config.pdf_dir)):
            cand = Path(root) / rel
            if cand.exists():
                return str(cand.resolve())
        # 文件已不存在，回退到笔记目录（仅用于状态重建，实际删除操作会后续清理）
        return str(Path(self.service.config.note_dir) / rel)

    def _read_state(self) -> tuple[int, dict] | None:
        """读原始状态文件，返回 (版本号, docs)。

        返回值不做版本判断——版本门控由调用方决定（增量更新要求同版本，
        index_all 遇到不符版本则全量重扫）。

        Returns:
            tuple[int, dict] | None: (版本号, {绝对路径: mtime})；文件不存在或
            JSON 非法返回 None。旧格式（裸 {abs_path: mtime}，无 version 字段）
            按版本 0 处理，保证升级路径上旧状态被识别为「不符」而非崩溃。
        """
        if not self._state_path.exists():
            return None
        try:
            raw = json.loads(self._state_path.read_text())
        except (json.JSONDecodeError, OSError):
            # JSON 损坏等同于状态缺失：调用方走全量重扫，绝不让坏文件炸掉索引
            return None
        if isinstance(raw, dict) and isinstance(raw.get("docs"), dict):
            return int(raw.get("version", 0)), dict(raw["docs"])
        # 旧格式（裸 {abs_path: mtime}）按版本 0 处理
        return 0, dict(raw) if isinstance(raw, dict) else {}

    def _load_state(self) -> dict:
        """读状态 docs；文件缺失或版本不符时返回空 dict（触发全量重扫）。

        Returns:
            dict: {绝对路径: mtime}；版本门控不通过时为空 dict。
        """
        raw = self._read_state()
        return raw[1] if raw and raw[0] == _STATE_VERSION else {}

    def _save_state(self, state: dict) -> None:
        """把状态按当前版本格式写入（自动创建父目录）。

        Args:
            state: {绝对路径: mtime} 映射；版本号由本方法统一补上，
                   调用方只管 docs 内容。
        """
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(
            json.dumps({"version": _STATE_VERSION, "docs": state}))

    # ---------- 文档解析 → 分块 ----------
    def _parse_file(self, path: Path) -> _FileContent:
        """读取并解析文档，产出切块所需的全部原料。

        - PDF: 解析器（GROBID 优先 / PyMuPDF 回退）给出章节、表格、图注与主
          标题；回退路径 title/tables/figures 为空，切块退化为「章节标题前缀 +
          裸正文」，表格图注不产生块。
        - Markdown: 按 # 行切章节；标题取首个一级标题（"# " 开头）文本，
          绝不用文件名充当标题（与 TitleExtractor 同一纪律）。

        Args:
            path: 文档路径。

        Returns:
            _FileContent: source/title/sections/tables/figures 五元组。
        """
        if path.suffix.lower() == ".pdf":
            parsed = self.service.pdf_parser().parse_pdf(str(path))
            return _FileContent("pdf", parsed.title or "", parsed.sections,
                                parsed.tables, parsed.figures)

        # Markdown 笔记：按 # / ## 标题分段，顺带捕获首个一级标题作文档标题
        lines = path.read_text(encoding="utf-8").splitlines()
        title = ""
        sections: list[tuple[str, str]] = []
        cur_head, cur_body = "", []
        for ln in lines:
            if ln.startswith("#"):
                # 遇到新标题，保存当前章节（如果有内容）
                if cur_head or cur_body:
                    sections.append((cur_head, "\n".join(cur_body)))
                cur_head, cur_body = ln.lstrip("# "), [] # 去掉 # 和后面的空格
                # 标题只认 "# " 开头的一级标题，取第一个；## 及更深的不算
                if not title and ln.startswith("# "):
                    title = ln[2:].strip()
            else:
                cur_body.append(ln)
        # 保存最后一个章节
        if cur_head or cur_body:
            sections.append((cur_head, "\n".join(cur_body)))
        return _FileContent("note", title, sections, [], [])

    def _media_chunks(self, rel: str, parsed: _FileContent,
                      start_index: int) -> list[Chunk]:
        """把表格与图注转成独立检索块，接在章节块之后（ADR 0001「Table/Figure
        独立块」的实现——此前解析结果从未进索引）。

        GROBID 的表格文本是单元格拼接，先折叠连续空白压掉换行噪声；空白项
        不产生块；超长表格截断到 _TABLE_TEXT_LIMIT。前缀规则与章节块一致
        （{title} > [表格]/[图注]），id 沿用 sha1(rel:index) 幂等方案、序号顺延。

        Args:
            rel: 文档相对路径（进块 id 与元数据）。
            parsed: _parse_file 的解析产物。
            start_index: 起始块序号（章节块数量），保证与章节块的 id 空间不重叠。

        Returns:
            list[Chunk]: 表格块与图注块（可能为空）。
        """
        chunks: list[Chunk] = []
        idx = start_index

        def _add(heading: str, text: str) -> None:
            nonlocal idx
            # 折叠连续空白：GROBID 表格是单元格拼接，换行/多空格只是噪声，
            # 压平后对 BM25 分词和 embedding 都更干净
            text = " ".join(text.split())
            if not text:
                return
            chunk_id = hashlib.sha1(f"{rel}:{idx}".encode()).hexdigest()[:16]
            chunks.append(Chunk(
                id=chunk_id, text=context_prefix(parsed.title, heading, text),
                path=rel, source=parsed.source, heading=heading, chunk_index=idx,
            ))
            idx += 1

        for table in parsed.tables:
            _add("[表格]", table[:_TABLE_TEXT_LIMIT])
        for caption in parsed.figures:
            _add("[图注]", caption)
        return chunks

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

        向量库中每个块都存储了其所属文档的相对路径和修改时间。
        同一文档的多个块共享相同的修改时间，因此取最大值即可。

        Args:
            store: VectorStore 实例。

        Returns:
            dict: {绝对路径: 修改时间戳}，用于后续增量比对。
        """
        state: dict = {}
        for _id, _doc, rel, mtime in store.all_documents():
            abs_path = self._rel_to_abs(rel)
            # 同一文档可能有多块，取最新的 mtime
            if abs_path not in state or mtime > state[abs_path]:
                state[abs_path] = mtime
        return state

    # ---------- 公开 API ----------
    def index_document(self, path: str) -> None:
        """单篇文档的增量重索引入口，文档写入/编辑/下载完成后调用。

        为什么必须"先删后建"？
        - 块 ID 由相对路径和块序号哈希生成，与内容无关。
        - 若文档内容缩短（如删掉某些章节），旧块 ID 不再出现，若不清除则会永久残留。
        - 因此每次索引必须先清除旧块，再写入新块。

        边界条件处理：
        - 若文件不存在，直接返回（静默跳过）。
        - 若文件不在知识库根目录（如记忆目录），返回（避免跨目录同名文件冲突）。
        - 若文档切块后为空（如全空白或只有参考文献），删除旧块后不写入新块，状态文件不更新。
        - 若文档内容未变（修改时间未变），外部调用方应避免调用本方法（但本方法本身不检查）。

        状态版本门控：仅当状态文件缺失或已是当前版本时才写入状态。版本不符
        说明存在旧格式状态、待 index_all 全量重扫（旧状态里还留着其他文档的
        记录，是删除清理的依据），此时覆盖写单篇状态会抹掉这份依据（半更新），
        让下次全量重扫的清理环节失效。

        Args:
            path: 文档的绝对路径（或相对路径，会被解析）。
        """
        p = Path(path)
        if not p.exists():
            return                      # 索引不存在的文件：直接跳过

        rel = self._rel_path(str(p))
        if rel is None:
            # 非知识库根目录的路径（如记忆目录）直接跳过，避免与同名笔记冲突。
            # 记忆文件的写入也会触发本钩子，但本模块只索引笔记和 PDF；
            # 若不跳过，记忆目录下与笔记目录同名的文件会撞上同一个相对路径和块 id，导致笔记的块被静默覆盖删除。
            return

        # 1. 解析文档，获得切块原料（章节 + 标题 + 表格图注）
        parsed = self._parse_file(p)
        store = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()

        # 2. 清除该文档的旧索引（定点查询替代全表扫描）
        old_ids = store.doc_chunk_ids(rel)
        for did in old_ids:
            bm25.remove_document(did)
        store.delete_doc(rel) # 按相对路径删除所有块

        # 3. 切分：章节块 + 表格/图注块，过滤空白块
        chunks = self.service.chunker.split_doc(rel, parsed.sections, parsed.source,
                                                title=parsed.title)
        chunks.extend(self._media_chunks(rel, parsed, start_index=len(chunks)))
        chunks = [c for c in chunks if c.text.strip()]   # 过滤空白文本的块，避免产生无意义向量
        if not chunks:
            # 文档被清空：旧块已删，无需写新内容
            return

        # 4. 编码并写入
        vecs = self._embed_chunks(chunks)
        mtime = p.stat().st_mtime
        store.upsert(chunks, vecs, mtime=mtime)
        bm25.add_documents([(c.id, c.text) for c in chunks])

        # 5. 更新状态文件——仅「状态缺失或同版本」时写入。状态缺失时没有可
        #    丢失的清理依据，正常写入让热更新路径的状态记录照常生效；版本
        #    不符时旧状态是 index_all 删除清理的依据，覆盖写会造成半更新
        #    （详见 docstring 的状态版本门控说明）。
        raw = self._read_state()
        if raw is None or raw[0] == _STATE_VERSION:
            docs = raw[1] if raw else {}
            docs[str(p.resolve())] = mtime
            self._save_state(docs)

    def index_all(self) -> None:
        """全量增量扫描：只重索引新增或变更的文档，并清理已删除的文档。

        这是 `index_document` 的批量版本，适用于启动时或定时任务。

        状态版本门控（P6）：
        - 状态文件缺失、JSON 损坏或版本号与 _STATE_VERSION 不符（切块逻辑
          升级后的首次运行）→ 放弃旧状态，全量重扫重嵌。
        - 不从向量库元数据恢复旧版本状态：库内块无法确认由当前版本的切块
          逻辑产出，恢复会让旧配方块因 mtime 未变而永久残留。

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
        """
        store = self.service._ensure_vector_store()
        raw = self._read_state()

        if raw is None or raw[0] != _STATE_VERSION:
            # 状态缺失或版本不符（含切块逻辑升级后的首次运行）：放弃旧状态，
            # 全量重扫重嵌。不能从向量库元数据恢复——库内块无法确认由当前版本
            # 的切块逻辑产出，恢复会让旧配方块因 mtime 未变而永久残留。
            state = {}
        else:
            state = raw[1]
            # 同版本下保留原有两兜底：向量库被清空 → 状态作废；状态空 → 从元数据恢复
            if store.count() == 0 and state:
                state = {}
            elif not state and store.count() > 0:
                state = self._derive_state_from_store(store)

        # 2. 从向量库的全部文档整体重建 BM25（因为 BM25 是内存索引，进程重启后为空）。
        self.service._ensure_bm25().rebuild(
            [(d[0], d[1]) for d in store.all_documents()])

        # 收集待索引文档：扫描两个知识库根目录，按修改时间比对找出变更项。
        roots = [Path(self.service.config.note_dir),
                 Path(self.service.config.pdf_dir)]
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

                # 4. 对比状态文件中的修改时间，筛选出变更的文件。
                mtime = p.stat().st_mtime
                if state.get(str(p.resolve())) != mtime:
                    changed.append(p)
                new_state[str(p.resolve())] = mtime

        # 5. 从状态中找出已删除的文件（状态里有记录但本次扫描没见到的）。
        removed = [k for k in state if k not in {str(s) for s in seen}]

        # 6. 清理已删除的文档。
        for abs_path in removed:
            rel = self._rel_path(abs_path)
            # 防御性跳过（旧版本可能残留非知识库路径）
            if rel is None:
                # 防御性兜底：正常流程下状态文件里的键只可能是笔记/PDF 目录
                # 路径（写入时就过滤过），但旧版本可能残留其他目录的键——
                # 遇到时跳过，不要误删对应块。
                continue
            # 定点查询该文档的所有块 ID（替代全表扫描后按路径过滤）
            rm_ids = store.doc_chunk_ids(rel)
            for did in rm_ids:
                self.service._ensure_bm25().remove_document(did)
            store.delete_doc(rel)

        # 7. 增量索引变更的文档。
        for p in changed:
            self.index_document(str(p))

        # 8. 保存新的状态文件（_save_state 自动带当前版本号）。
        self._save_state(new_state)
