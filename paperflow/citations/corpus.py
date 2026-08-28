"""语料标题索引：note H1 + PDF 解析标题 → 论文中心索引（易变投影）。

corpus_titles.json 是「语料里有哪些论文」的快照，供引用解析做全标题精确
匹配。标题提取**复用现有代码**（ParsedDoc.title / TitleExtractor 5 级链），
不新写提取逻辑。索引按 (path, mtime_ns) 增量重建：只对新增/变更文件重提
标题，删除的文件从索引移除。索引是易变投影——bib 才是稳定真相源。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class CorpusIndex:
    """论文中心索引：`{norm_title: {title, note_path, pdf_path, biblio}}`。

    一篇论文可同时有笔记与 PDF（同全标题合并成一条记录）。biblio 仅当 PDF
    解析出书目元数据时存在；笔记只贡献 note_path + title。
    """

    def __init__(self, config, rag_service=None, title_extractor=None):
        """注入 config；rag_service/title_extractor 可注入桩（测试），缺省惰性获取。

        rag_service 提供 parse_pdf_cached（复用 GROBID 解析）；title_extractor
        是 TitleExtractor（PDF 解析无标题时的 5 级链兜底）。
        """
        self.config = config
        self._rag_service = rag_service
        self._title_extractor = title_extractor
        self._lock = threading.RLock()
        self._cache_path = Path(config.workspace) / "citations" / "corpus_titles.json"
        self._records: dict[str, dict] = {}   # norm_title -> 论文记录
        self._mtime: dict[str, int] = {}      # path -> mtime_ns（增量判据）

    # —— 惰性依赖（RAG 式，首次访问才构造重组件）——
    def _rag(self):
        if self._rag_service is None:
            from paperflow.rag.services.rag_service import get_rag_service
            self._rag_service = get_rag_service()
        return self._rag_service

    def _te(self):
        if self._title_extractor is None:
            from paperflow.core.memory.services.title_extractor import TitleExtractor
            from paperflow.rag.parsers.grobid_client import GrobidClient
            self._title_extractor = TitleExtractor(grobid=GrobidClient(self.config.grobid_endpoint))
        return self._title_extractor

    @staticmethod
    def normalize(title: str) -> str:
        """标题归一化（与 bib._normalize 同规则：小写+去标点+折叠空白）。"""
        import re
        return re.sub(r"[\s\W_]+", "", title.lower())

    # —— 索引生命周期 ——
    def refresh(self) -> None:
        """增量重建：扫描 vault 目录，只对新增/变更文件重提标题，删除的移除。"""
        with self._lock:
            current: dict[str, int] = {}
            for root, pattern, kind in ((self.config.vault_note_dir, "*.md", "note"),
                                        (self.config.vault_pdf_dir, "*.pdf", "pdf")):
                if not root:
                    continue
                for p in Path(root).rglob(pattern):
                    st = p.stat()
                    path = str(p)
                    current[path] = st.st_mtime_ns
                    if self._mtime.get(path) != st.st_mtime_ns:
                        self._upsert(path, kind)
            for path in list(self._mtime):
                if path not in current:
                    self._remove(path)
            self._mtime = current
            self.save()

    def load(self) -> None:
        """从磁盘载入缓存（refresh 前调用则复用上次索引）。"""
        if self._cache_path.exists():
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self._records = data.get("records", {})
            self._mtime = {k: int(v) for k, v in data.get("mtime", {}).items()}

    def save(self) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(
            {"records": self._records, "mtime": self._mtime}, ensure_ascii=False), encoding="utf-8")

    def _upsert(self, path: str, kind: str) -> None:
        if kind == "pdf":
            title, biblio = self._pdf_meta(path)
        else:
            title, biblio = self._note_title(path), {}
        if not title:
            return  # 提不出标题的不索引（如损坏 PDF）
        norm = self.normalize(title)
        field = "pdf_path" if kind == "pdf" else "note_path"
        # 文件标题变更时（同路径 mtime 变化），先把本路径从其他记录的引用里
        # 解绑，避免新旧两个标题同时指向同一文件（ghost record）
        for other in self._records.values():
            if other.get(field) == path:
                other[field] = None
        rec = self._records.setdefault(norm, {"title": title, "note_path": None,
                                              "pdf_path": None, "biblio": {}})
        rec["title"] = title
        rec[field] = path
        if kind == "pdf" and biblio:
            rec["biblio"] = biblio

    def _remove(self, path: str) -> None:
        """从所有包含该路径的记录里摘除引用；记录空壳则删除。"""
        for norm, rec in list(self._records.items()):
            if rec.get("pdf_path") == path:
                rec["pdf_path"] = None
            if rec.get("note_path") == path:
                rec["note_path"] = None
            if not rec.get("pdf_path") and not rec.get("note_path"):
                del self._records[norm]

    def _pdf_meta(self, path: str) -> tuple[str, dict]:
        """标题+书目：复用 GROBID 解析（ParsedDoc.title/biblio），空时回退 TitleExtractor。

        解析失败（异常/空标题）走 5 级链兜底；全失败返回空串——调用方决定
        是否索引（不索引比用文件名凑数好）。
        """
        try:
            doc = self._rag().parse_pdf_cached(path)
            if doc.title:
                return doc.title, doc.biblio
        except Exception:
            pass
        try:
            r = self._te().extract(pdf_path=path)
            if r.title:
                return r.title, {}
        except Exception:
            pass
        return "", {}

    @staticmethod
    def _note_title(path: str) -> str:
        """笔记标题：首行 `# ...`（笔记 H1 是权威标题）。"""
        try:
            first = Path(path).read_text(encoding="utf-8").splitlines()[0].strip()
            return first.lstrip("#").strip()
        except Exception:
            return ""

    # —— 查询 ——
    def match(self, title: str) -> dict | None:
        """按全标题归一化精确匹配；无命中返回 None。"""
        return self._records.get(self.normalize(title))

    def record_by_path(self, path: str) -> dict | None:
        """按 note/pdf 路径反查论文记录（source path 兜底入口）。"""
        p = str(Path(path).resolve())
        for rec in self._records.values():
            if rec.get("pdf_path") == p or rec.get("note_path") == p:
                return rec
        return None

    def record_by_title(self, title: str) -> dict | None:
        return self.match(title)
