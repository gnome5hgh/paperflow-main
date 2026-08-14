"""RagIndexer：把知识库里的文档（Markdown 笔记和 PDF）索引进向量库与 BM25。

索引方式是增量扫描：用文件的修改时间判断哪些文档变了，只重索引新增或
变更的文档；被删除的文档则从索引里清理掉。

幂等设计：单篇文档的重新索引用"先删后建"。因为块 id 由路径加序号哈希
生成、与内容无关，文档内容收缩或删掉某些章节时，原来位置上的旧块必须
显式清除，否则会永远残留在索引里。BM25 是向量库文档在内存里的投影，
删除和写入必须与向量库成对执行才能保持一致。
"""
import json
from pathlib import Path

from paperflow.rag.parsers.chunker import Chunk


class RagIndexer:
    """索引器：维护"文档路径 → 修改时间"的状态文件，据此做增量索引。"""

    def __init__(self, service):
        """绑定门面服务，并定位索引状态文件（工作区下的 index_state.json）。

        service 必须是 RAGService 单例——索引器是它的一个视图，底层向量库 /
        BM25 / 编码器都经 service 惰性获取，与检索器共享同一批组件。
        """
        self.service = service
        # 状态文件放在工作区下，记录每个已索引文档的修改时间
        self._state_path = Path(service.config.workspace) / "index_state.json"

    # —— 路径/状态工具 ——
    def _rel_path(self, path: str) -> str | None:
        """把绝对路径转成相对知识库根目录的路径（用于文档 id 与元数据，跨机器稳定）。

        如果路径既不在笔记目录也不在 PDF 目录下，返回 None——不能用文件名代替。
        原因：还有其他目录（如记忆目录）里的文件也会触发索引钩子，若用文件名
        代替，与笔记目录里同名的文件（如 memory/shared.md 与 note/shared.md）
        会得到相同的相对路径和块 id，导致后索引的文档静默覆盖、删除前者的块，
        造成数据丢失。本模块只索引笔记和 PDF，非这两个目录的路径必须返回
        None，由调用方跳过处理。
        """
        abs_path = Path(path).resolve()
        for root in (Path(self.service.config.vault_note_dir).resolve(),
                     Path(self.service.config.vault_pdf_dir).resolve()):
            try:
                return str(abs_path.relative_to(root))
            except ValueError:
                continue
        return None

    def _rel_to_abs(self, rel: str) -> str:
        """把相对知识库根目录的路径还原成绝对路径（从向量库元数据重建索引状态时用）。"""
        for root in (Path(self.service.config.vault_note_dir),
                     Path(self.service.config.vault_pdf_dir)):
            cand = Path(root) / rel
            if cand.exists():
                return str(cand.resolve())
        # 文件已不存在（被删）时无法确定它在哪个根目录，改用笔记目录。
        # 该路径本就不会出现在本次扫描的结果里，后续的删除清理会兜底移除对应块。
        return str(Path(self.service.config.vault_note_dir) / rel)

    def _load_state(self) -> dict:
        """读取索引状态文件；不存在时返回空字典。"""
        if self._state_path.exists():
            return json.loads(self._state_path.read_text())
        return {}

    def _save_state(self, state: dict) -> None:
        """把索引状态写入状态文件（自动创建父目录）。"""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state))

    # —— 文档解析 → chunks ——
    def _sections_from_file(self, path: Path) -> tuple[str, list[tuple[str, str]]]:
        """读取文件并切成章节，返回 (来源, 章节列表)。

        PDF 走 GROBID（不可用则改用启发式解析）；Markdown 笔记按 # 标题分段。
        """
        if path.suffix.lower() == ".pdf":
            parsed = self.service.pdf_parser().parse_pdf(str(path))
            return "pdf", parsed.sections
        # Markdown 笔记：按 # / ## 标题分段
        lines = path.read_text(encoding="utf-8").splitlines()
        sections: list[tuple[str, str]] = []
        cur_head, cur_body = "", []
        for ln in lines:
            if ln.startswith("#"):
                if cur_head or cur_body:
                    sections.append((cur_head, "\n".join(cur_body)))
                cur_head, cur_body = ln.lstrip("# "), []
            else:
                cur_body.append(ln)
        if cur_head or cur_body:
            sections.append((cur_head, "\n".join(cur_body)))
        return "note", sections

    def _embed_chunks(self, chunks: list[Chunk]):
        """把一批块文本编码成向量（供写入向量库）。"""
        embedder = self.service._ensure_embedder()
        vecs = embedder([c.text for c in chunks])
        return vecs

    def _derive_state_from_store(self, store) -> dict:
        """从向量库的元数据重建索引状态：{绝对路径: 修改时间}。

        适用场景：状态文件丢失但向量库还在。修改时间存在每个块的元数据里，
        据此重建后仍可继续做增量比对，内容没变的文档不会被重新编码。
        同一文档的多个块共享同一个修改时间，取最大值即可。
        """
        state: dict = {}
        for _id, _doc, rel, mtime in store.all_documents():
            abs_path = self._rel_to_abs(rel)
            if abs_path not in state or mtime > state[abs_path]:
                state[abs_path] = mtime
        return state

    # —— 公开 API ——
    def index_document(self, path: str) -> None:
        """单篇文档的增量重索引入口，文档写入/编辑/下载完成后调用。

        文档级幂等：先把该文档的全部旧块从向量库和 BM25 中删掉，再重新
        切块、编码、写入。因为块 id 由路径加序号哈希生成、与内容无关，
        文档收缩时消失位置上的旧块必须显式清除，否则会残留。
        """
        p = Path(path)
        if not p.exists():
            return                      # 索引不存在的文件：直接跳过
        rel = self._rel_path(str(p))
        if rel is None:
            # 非知识库根目录的路径（如记忆目录）直接跳过。记忆文件的写入
            # 也会触发本钩子，但本模块只索引笔记和 PDF；若不跳过，记忆目录下
            # 与笔记目录同名的文件会撞上同一个相对路径和块 id，导致笔记的块
            # 被静默覆盖删除。
            return
        source, sections = self._sections_from_file(p)
        store = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()
        # 文档级幂等：先删该文档全部旧块（向量库 + BM25），再重切重写
        old_ids = [d[0] for d in store.all_documents() if d[2] == rel]
        for did in old_ids:
            bm25.remove_document(did)
        store.delete_doc(rel)
        chunks = self.service.chunker.split_doc(rel, sections, source)
        chunks = [c for c in chunks if c.text.strip()]   # 过滤空白文本的块，避免产生无意义向量
        if not chunks:
            return                          # 文档被清空：旧块已删，无需写新内容
        vecs = self._embed_chunks(chunks)
        mtime = p.stat().st_mtime
        store.upsert(chunks, vecs, mtime=mtime)
        bm25.add_documents([(c.id, c.text) for c in chunks])
        state = self._load_state()
        state[str(p.resolve())] = mtime
        self._save_state(state)

    def index_all(self) -> None:
        """全量增量扫描：只重索引新增或变更的文档，并清理已删除的文档。"""
        store = self.service._ensure_vector_store()
        state = self._load_state()
        # 一致性兜底 1：向量库为空但状态文件里有记录 → 说明索引被清空过，
        # 清空状态，让本次扫描走全量重扫。
        if store.count() == 0 and state:
            self._save_state({})
            state = {}
        # 一致性兜底 2：状态文件丢失但向量库还在 → 从向量库元数据重建状态
        #（含修改时间），保证内容没变的文档不会被重新编码。
        elif not state and store.count() > 0:
            state = self._derive_state_from_store(store)
            self._save_state(state)
        # BM25 只驻留内存、进程重启后为空 → 从向量库的全部文档整体重建。
        self.service._ensure_bm25().rebuild(
            [(d[0], d[1]) for d in store.all_documents()])
        # 收集待索引文档：扫描两个知识库根目录，按修改时间比对找出变更项。
        roots = [Path(self.service.config.vault_note_dir),
                 Path(self.service.config.vault_pdf_dir)]
        new_state: dict = {}
        changed: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not p.is_file() or p.suffix.lower() not in (".md", ".pdf"):
                    continue
                seen.add(p.resolve())
                mtime = p.stat().st_mtime
                if state.get(str(p.resolve())) != mtime:
                    changed.append(p)
                new_state[str(p.resolve())] = mtime
        # 删除清理：状态里有记录、但本次扫描没见到的 → 逐个移除其块（无需整库重建）
        removed = [k for k in state if k not in {str(s) for s in seen}]
        for abs_path in removed:
            rel = self._rel_path(abs_path)
            if rel is None:
                # 防御性兜底：正常流程下状态文件里的键只可能是笔记/PDF 目录
                # 路径（写入时就过滤过），但旧版本可能残留其他目录的键——
                # 遇到时跳过，不要误删对应块。
                continue
            rm_ids = [d[0] for d in store.all_documents() if d[2] == rel]
            for did in rm_ids:
                self.service._ensure_bm25().remove_document(did)
            store.delete_doc(rel)
        # 增量索引变更文档
        for p in changed:
            self.index_document(str(p))
        self._save_state(new_state)
