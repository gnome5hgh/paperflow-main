"""RagIndexer：mtime 增量扫描 + index_document 热更新 + authority 双守卫。

调用点边界：index_all() 本层只交付"可调用 + 被测"；真正挂进启动流程是
Layer 4 CLI bootstrap，Layer 2 不挂启动钩子。
"""
import json
from pathlib import Path

from paperflow.rag.chunker import Chunk


class RagIndexer:
    def __init__(self, service):
        self.service = service
        self._state_path = Path(service.config.workspace) / "index_state.json"

    # —— 路径/状态工具 ——
    def _rel_path(self, path: str) -> str:
        """绝对路径 → 相对 vault 根（doc id 与 metadata 共用，跨机器稳定）。"""
        abs_path = Path(path).resolve()
        for root in (Path(self.service.config.vault_note_dir).resolve(),
                     Path(self.service.config.vault_pdf_dir).resolve()):
            try:
                return str(abs_path.relative_to(root))
            except ValueError:
                continue
        return abs_path.name

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

    # —— 公开 API ——
    def index_document(self, path: str) -> None:
        """热更新钩子：单个文档立即重索引 + BM25 增量（Write/Edit/下载后调用）。"""
        p = Path(path)
        if not p.exists():
            return                      # 索引不存在的文件 → no-op
        source, sections = self._sections_from_file(p)
        rel = self._rel_path(str(p))
        chunks = self.service.chunker.split_doc(rel, sections, source)
        chunks = [c for c in chunks if c.text.strip()]   # 过滤空文本 chunk（degenerate embedding 防护）
        if not chunks:
            return
        vecs = self._embed_chunks(chunks)
        mtime = p.stat().st_mtime
        self.service._ensure_vector_store().upsert(chunks, vecs, mtime=mtime)
        self.service._ensure_bm25().add_documents([(c.id, c.text) for c in chunks])
        state = self._load_state()
        state[str(p.resolve())] = mtime
        self._save_state(state)

    def index_all(self) -> None:
        """增量扫描：mtime 比对，只重索引新增/变更；删除的文档清理。"""
        store = self.service._ensure_vector_store()
        state = self._load_state()
        # authority：collection 计数=0 且 state 非空 → 视为 chromadb 被清空，全量重扫
        if store.count() == 0 and state:
            self._save_state({})
            state = {}
        # authority：ChromaDB 在、state 丢 → 从 documents 重建 BM25（不重扫）
        if not state and store.count() > 0:
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
        # 删除清理：state 有、本次未见 → 从向量库删除 + BM25 全量重建
        removed = [k for k in state if k not in {str(s) for s in seen}]
        for abs_path in removed:
            store.delete_doc(self._rel_path(abs_path))
        if removed:
            self.service._ensure_bm25().rebuild(
                [(d[0], d[1]) for d in store.all_documents()])
        # 增量索引变更文档
        for p in changed:
            self.index_document(str(p))
        self._save_state(new_state)
