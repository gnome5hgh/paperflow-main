"""RagIndexer：mtime 增量扫描 + index_document 热更新 + authority 双守卫。

调用点边界：index_all() 本层只交付"可调用 + 被测"；真正挂进启动流程是
Layer 4 CLI bootstrap，Layer 2 不挂启动钩子。

幂等设计：index_document 采用文档级 delete-then-reindex——chunk id 与内容无关
（sha1(path:idx)），note 收缩/删章节时消失位置的旧块必须显式清除，否则永远残留。
BM25 是 ChromaDB 的内存投影，增量双写（remove_document + add_documents）保持对齐。
"""
import json
from pathlib import Path

from paperflow.rag.chunker import Chunk


class RagIndexer:
    def __init__(self, service):
        self.service = service
        self._state_path = Path(service.config.workspace) / "index_state.json"

    # —— 路径/状态工具 ——
    def _rel_path(self, path: str) -> str | None:
        """绝对路径 → 相对 vault 根（doc id 与 metadata 共用，跨机器稳定）。

        两个 vault 根（note/pdf）都不匹配时返回 None——**不**回退 basename。
        修复 CRITICAL-1：memory 根（WriteFileTool 的 _NOTE_ROOTS 含 memory）
        此前回退 abs_path.name，与 note 下同名文件（memory/shared.md vs
        note/shared.md）得到同一 rel → 同一 chunk id → index_document 静默
        覆盖并删除 note chunks（数据丢失）。RAG 只索引 note/pdf（SCOPE），
        非 vault 路径必须返回 None 由调用方 no-op。"""
        abs_path = Path(path).resolve()
        for root in (Path(self.service.config.vault_note_dir).resolve(),
                     Path(self.service.config.vault_pdf_dir).resolve()):
            try:
                return str(abs_path.relative_to(root))
            except ValueError:
                continue
        return None

    def _rel_to_abs(self, rel: str) -> str:
        """相对 vault 根 → 绝对路径（guard-2 从 metadata 重建 state 用）。"""
        for root in (Path(self.service.config.vault_note_dir),
                     Path(self.service.config.vault_pdf_dir)):
            cand = Path(root) / rel
            if cand.exists():
                return str(cand.resolve())
        # 文件已不存在（被删）时无法定位实际根，回退 note 根——该路径本就不会
        # 出现在本次 seen 中，后续删除清理会兜底移除对应块
        return str(Path(self.service.config.vault_note_dir) / rel)

    def _load_state(self) -> dict:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text())
        return {}

    def _save_state(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state))

    # —— 文档解析 → chunks ——
    def _sections_from_file(self, path: Path) -> tuple[str, list[tuple[str, str]]]:
        """返回 (source, sections)；note 按 Markdown 标题切，pdf 走 GROBID/回退。"""
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
        embedder = self.service._ensure_embedder()
        vecs = embedder([c.text for c in chunks])
        return vecs

    def _derive_state_from_store(self, store) -> dict:
        """从 ChromaDB metadata 重建 state：{abs_path: mtime}。

        guard-2 用：state 文件丢失但 ChromaDB 在，mtime 存于 metadata，
        据此重建后可继续 mtime diff，不变文档不重 embedding（兑现"不重扫"）。
        同一文档多块共享同一 mtime，取最大即可。"""
        state: dict = {}
        for _id, _doc, rel, mtime in store.all_documents():
            abs_path = self._rel_to_abs(rel)
            if abs_path not in state or mtime > state[abs_path]:
                state[abs_path] = mtime
        return state

    # —— 公开 API ——
    def index_document(self, path: str) -> None:
        """热更新钩子：单个文档立即重索引 + BM25 增量（Write/Edit/下载后调用）。

        文档级幂等：先删该文档全部旧块（ChromaDB + BM25），再重切重写。
        chunk id 与内容无关（sha1(path:idx)），note 收缩时消失位置的旧块必须显式清除。
        """
        p = Path(path)
        if not p.exists():
            return                      # 索引不存在的文件 → no-op
        rel = self._rel_path(str(p))
        if rel is None:
            # CRITICAL-1 修复：非 vault 根路径（如 memory/ 下）→ no-op。
            # memory 写会触发本钩子，但 RAG 只索引 note/pdf（SCOPE）——跳过，
            # 否则 memory/shared.md 与 note/shared.md 撞同一 rel/doc id 会
            # 覆盖并删除 note chunks。
            return
        source, sections = self._sections_from_file(p)
        store = self.service._ensure_vector_store()
        bm25 = self.service._ensure_bm25()
        # 文档级幂等：先删该文档全部旧块（ChromaDB + BM25），再重切重写
        old_ids = [d[0] for d in store.all_documents() if d[2] == rel]
        for did in old_ids:
            bm25.remove_document(did)
        store.delete_doc(rel)
        chunks = self.service.chunker.split_doc(rel, sections, source)
        chunks = [c for c in chunks if c.text.strip()]   # 过滤空文本 chunk（degenerate embedding 防护）
        if not chunks:
            return                          # 文档被清空 → 旧块已删，无需写新
        vecs = self._embed_chunks(chunks)
        mtime = p.stat().st_mtime
        store.upsert(chunks, vecs, mtime=mtime)
        bm25.add_documents([(c.id, c.text) for c in chunks])
        state = self._load_state()
        state[str(p.resolve())] = mtime
        self._save_state(state)

    def index_all(self) -> None:
        """增量扫描：mtime 比对，只重索引新增/变更；删除的文档清理。"""
        store = self.service._ensure_vector_store()
        state = self._load_state()
        # authority：collection 空 + state 在 → 全量重扫（清 state）
        if store.count() == 0 and state:
            self._save_state({})
            state = {}
        # authority：state 丢 + ChromaDB 在 → 从 metadata 重建 state（含 mtime），
        # 不变文档不重 embedding（兑现 spec §10 "不重扫"）
        elif not state and store.count() > 0:
            state = self._derive_state_from_store(store)
            self._save_state(state)
        # 启动投影：BM25 是内存索引、进程重启后为空 → 从 ChromaDB documents 整体重建
        self.service._ensure_bm25().rebuild(
            [(d[0], d[1]) for d in store.all_documents()])
        # 收集待索引文档
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
        # 删除清理：state 有、本次未见 → 逐孤儿移除（不用整库重建）
        removed = [k for k in state if k not in {str(s) for s in seen}]
        for abs_path in removed:
            rel = self._rel_path(abs_path)
            if rel is None:
                # CRITICAL-1 修复的防御性兜底：state 键经 _rel_path 修复后
                # 只可能是 vault 路径（index_document 不再写入非 vault 键），
                # 但历史 state 可能残留 memory 键——rel=None 时跳过，勿误删
                continue
            rm_ids = [d[0] for d in store.all_documents() if d[2] == rel]
            for did in rm_ids:
                self.service._ensure_bm25().remove_document(did)
            store.delete_doc(rel)
        # 增量索引变更文档
        for p in changed:
            self.index_document(str(p))
        self._save_state(new_state)
