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

#: TitleExtractor 回退层级中可信任的来源：只有这些层级产出的标题可入索引/建条目。
#: pymupdf 字体启发式会把期刊名/页眉/arXiv 头当标题（GROBID 不可用时会污染 corpus
#: 与 bib），宁缺毋滥——提取不可靠就返回空，让调用方不索引/不建条目。
_TRUSTED_TITLE_SOURCES = frozenset({"grobid", "pdftitle"})


class CorpusIndex:
    """论文中心索引：`{norm_title: {title, note_path, pdf_path, biblio}}`。

    一篇论文可同时有笔记与 PDF（同全标题合并成一条记录）。biblio 仅当 PDF
    解析出书目元数据时存在；笔记只贡献 note_path + title。

    Attributes:
        config: PaperFlowConfig，语料目录与 GROBID 端点来源
        _rag_service: RAGService | None，惰性获取（提供 parse_pdf_cached）
        _title_extractor: TitleExtractor | None，惰性获取（PDF 无标题时的 5 级链兜底）
        _lock: threading.RLock，保护索引重建与查询
        _cache_path: Path，语料标题缓存文件（workspace/citations/corpus_titles.json）
        _records: dict[str, dict]，归一化标题 → 论文记录
        _mtime: dict[str, int]，文件路径 → mtime_ns（增量重建判据）
    """

    def __init__(self, config, rag_service=None, title_extractor=None):
        """注入 config；rag_service/title_extractor 可注入桩（测试），缺省惰性获取。

        rag_service 提供 parse_pdf_cached（复用 GROBID 解析）；
        title_extractor 是 TitleExtractor（PDF 解析无标题时的 5 级链兜底）。

        Args:
            config: PaperFlowConfig，配置来源
            rag_service: RAGService | None，可注入桩（缺省惰性获取）
            title_extractor: TitleExtractor | None，可注入桩（缺省惰性获取）
        """
        self.config = config
        self._rag_service = rag_service
        self._title_extractor = title_extractor
        self._lock = threading.RLock()
        self._cache_path = Path(config.runtime.workspace) / "citations" / "corpus_titles.json"
        self._records: dict[str, dict] = {}   # norm_title -> 论文记录
        self._mtime: dict[str, int] = {}      # path -> mtime_ns（增量判据）

    # —— 惰性依赖（RAG 式，首次访问才构造重组件）——
    def _rag(self):
        """惰性获取 RAG 服务实例，仅在首次调用 PDF 解析时初始化。

        避免在仅使用笔记索引时加载重量级的 GROBID 客户端及相关依赖。
        """
        if self._rag_service is None:
            from paperflow.rag.services.rag_service import get_rag_service
            self._rag_service = get_rag_service()
        return self._rag_service

    def _te(self):
        """惰性获取 TitleExtractor 实例，作为 GROBID 解析失败时的回退。

        回退链包含 pdftitle/pymupdf 等层级，但最终入库前会经 _TRUSTED_TITLE_SOURCES 过滤。
        """
        if self._title_extractor is None:
            from paperflow.core.memory.services.title_extractor import TitleExtractor
            from paperflow.rag.parsers.grobid_client import GrobidClient
            self._title_extractor = TitleExtractor(grobid=GrobidClient(
                self.config.rag.grobid.endpoint,
                timeout=self.config.rag.grobid.timeout))
        return self._title_extractor

    @staticmethod
    def normalize(title: str) -> str:
        """标题归一化（与 bib._normalize 同规则：小写+去标点+折叠空白）。

        Args:
            title: str，待归一化标题

        Returns:
            与 bib._normalize 同规则的小写去标点折叠空白串。
        """
        import re
        return re.sub(r"[\s\W_]+", "", title.lower())

    # —— 索引生命周期 ——
    def refresh(self) -> None:
        """增量重建：扫描语料库目录，只对新增/变更文件重提标题，删除的移除。"""
        with self._lock:
            # 1. 分别遍历笔记目录（*.md）与 PDF 目录（*.pdf），收集当前所有文件的绝对路径与 mtime。
            current: dict[str, int] = {}
            for root, pattern, kind in ((self.config.corpus.note_dir, "*.md", "note"),
                                        (self.config.corpus.pdf_dir, "*.pdf", "pdf")):
                if not root:
                    continue

                for p in Path(root).rglob(pattern):
                    st = p.stat()
                    path = str(p)
                    current[path] = st.st_mtime_ns

                    # 2. 若某路径不在 _mtime 中，或其 mtime 发生变化，则调用 _upsert 重新提取标题。
                    # 仅当文件新增或内容变更时才重新提取，未变更的文件直接沿用缓存
                    if self._mtime.get(path) != st.st_mtime_ns:
                        self._upsert(path, kind)

            # 3. 对 _mtime 中存在但当前扫描未出现的路径，调用 _remove 摘除其引用。
            # 移除在 current 中不存在的文件（即被删除或移出的文件）
            for path in list(self._mtime):
                if path not in current:
                    self._remove(path)

            # 4. 更新 _mtime 为当前快照，并将 _records 持久化到磁盘缓存。
            self._mtime = current
            self.save()

    def load(self) -> None:
        """从磁盘载入缓存（refresh 前调用则复用上次索引）。"""
        if self._cache_path.exists():
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
            self._records = data.get("records", {})
            self._mtime = {k: int(v) for k, v in data.get("mtime", {}).items()}

    def save(self) -> None:
        """将当前内存索引持久化到 JSON 缓存文件。"""
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(
            {"records": self._records, "mtime": self._mtime}, ensure_ascii=False), encoding="utf-8")

    def _upsert(self, path: str, kind: str) -> None:
        """更新或插入一条记录：根据 kind 提取标题，写入或合并到 _records。

        关键算法/边界处理（Ghost Record 防御）：
            文件路径和标题都可能发生变化。若某 PDF 文件之前属于标题 A，现在标题变为 B，
            在更新 B 的记录前，必须先将该路径从标题 A 的记录中移除（置为 None），
            否则同一文件路径会同时出现在 A 和 B 两条记录中，形成幽灵记录。
            具体做法：遍历所有现有记录，若记录的 field（pdf_path/note_path）等于当前路径，
            则先将该字段置空，再写入新记录。

        Args:
            path: 文件绝对路径（字符串）。
            kind: "pdf" 或 "note"，决定提取方式与写入的字段。
        """
        if kind == "pdf":
            title, biblio = self._pdf_meta(path)
        else:
            title, biblio = self._note_title(path), {}
        # 若提不出有效标题，则不索引该文件（避免损坏文件或空标题污染索引）
        if not title:
            return  # 提不出标题的不索引（如损坏 PDF）
        norm = self.normalize(title)
        field = "pdf_path" if kind == "pdf" else "note_path"

        # 幽灵记录清理：将该路径从其他记录的相同字段引用中解绑
        for other in self._records.values():
            if other.get(field) == path:
                other[field] = None

        # 获取或创建归一化标题对应的记录，合并路径与书目元数据
        rec = self._records.setdefault(norm, {"title": title, "note_path": None,
                                              "pdf_path": None, "biblio": {}})
        rec["title"] = title # 更新标题为最新提取值（标题也可能修正）
        rec[field] = path
        if kind == "pdf" and biblio:
            rec["biblio"] = biblio

    def _remove(self, path: str) -> None:
        """从所有包含该路径的记录里摘除引用；记录空壳则删除。

        Args:
            path: 需要移除的文件路径。
        """
        for norm, rec in list(self._records.items()):
            if rec.get("pdf_path") == path:
                rec["pdf_path"] = None

            if rec.get("note_path") == path:
                rec["note_path"] = None

            # 记录中既无 PDF 也无笔记时，移除该记录（避免空壳占用归一化标题 key）
            if not rec.get("pdf_path") and not rec.get("note_path"):
                del self._records[norm]

    def _pdf_meta(self, path: str) -> tuple[str, dict]:
        """标题+书目：复用 GROBID 解析（ParsedDoc.title/biblio），空时回退 TitleExtractor。

        解析策略（可靠层级优先）：
            1. 优先使用 RAG 服务（内部调用 GROBID）解析，获得 ParsedDoc.title 与 biblio。
            2. 若 GROBID 返回空标题或抛出异常，则回退到 TitleExtractor（5 级链）。
            3. **关键过滤**：TitleExtractor 的结果中，只接受 source 为 "grobid" 或 "pdftitle"
               的层级。pymupdf 等字体启发式层级容易将期刊名、页眉、arXiv 头误识别为标题，
               会严重污染索引与 bib，因此宁缺毋滥——不可靠来源直接返回空串。

        Returns:
            (title, biblio)。若无法提取可靠标题，title 返回空字符串，调用方将不索引该 PDF。

        Args:
            path: str，PDF 文件路径
        """
        # 1. 首选 GROBID 解析（RAG 服务内部有缓存）
        try:
            doc = self._rag().parse_pdf_cached(path)
            if doc.title:
                return doc.title, doc.biblio
        except Exception:
            pass

        # 2. 回退到 TitleExtractor，但仅信任指定层级
        try:
            r = self._te().extract(pdf_path=path)
            # TitleExtractor 回退只接受 grobid/pdftitle 层级（见 _TRUSTED_TITLE_SOURCES）
            if r.title and r.source in _TRUSTED_TITLE_SOURCES:
                return r.title, {}
        except Exception:
            pass

        # 3. 不可靠或提取失败：返回空标题，调用方跳过索引
        return "", {}

    @staticmethod
    def _note_title(path: str) -> str:
        """从笔记文件中提取第一个一级标题（H1，即以 `# ` 开头的行）。

        边界/兼容性处理（针对 Obsidian Frontmatter）：
            Obsidian 笔记常以 YAML frontmatter 块开头（`---` 起始，`---` 闭合）。
            旧版实现直接读取首行，若首行为 `---`，归一化后变为空串，导致所有
            带 frontmatter 的笔记全部塌缩到同一空 key 记录，造成数据污染。
            改进后的逻辑：
                1. 若文件首行为 `---`，则扫描至下一个单独的 `---` 行，跳过该块。
                2. 跳过 frontmatter 后，取第一个以 `#` 开头的非空行，去掉 `#` 前缀返回。
                3. 无 frontmatter 的笔记（如 paperFlow 自动生成，首行即 `# 标题`）行为保持不变。

        Returns:
            提取出的标题字符串，若无法提取则返回空字符串。

        Args:
            path: str，笔记文件路径
        """
        try:
            lines = Path(path).read_text(encoding="utf-8").splitlines()
        except Exception:
            return ""
        if lines and lines[0].strip() == "---":
            # 首行是 frontmatter 起始：找到闭合 `---`，其后才是正文
            for i in range(1, len(lines)):
                if lines[i].strip() == "---":
                    lines = lines[i + 1:]
                    break
        # 遍历正文行，找第一个 H1 标题行
        for line in lines:
            line = line.strip()
            if line.startswith("#"):
                return line.lstrip("#").strip()
        return ""

    # —— 查询 ——
    def pdf_records(self) -> list[dict]:
        """返回带 PDF 的论文记录（批量入库用）；调用方负责先 refresh()。

        Returns:
            记录 dict 的副本列表，每条含 title/pdf_path/note_path/biblio；
            纯笔记记录（pdf_path 为空）不返回。
        """
        with self._lock:
            return [dict(rec) for rec in self._records.values() if rec.get("pdf_path")]

    def match(self, title: str) -> dict | None:
        """按全标题归一化精确匹配；无命中返回 None。

        Args:
            title: str，论文全标题

        Returns:
            该标题对应的论文记录 dict；无命中返回 None。
        """
        return self._records.get(self.normalize(title))

    def record_by_path(self, path: str) -> dict | None:
        """按 note/pdf 路径反查论文记录（source path 兜底入口）。

        Args:
            path: str，note/pdf 文件路径

        Returns:
            命中该路径的论文记录 dict；无命中返回 None。
        """
        p = str(Path(path).resolve())
        for rec in self._records.values():
            if rec.get("pdf_path") == p or rec.get("note_path") == p:
                return rec
        return None

    def record_by_title(self, title: str) -> dict | None:
        """按标题查询记录（同 match 的别名，保持接口语义一致性）。

        Args:
            title: str，论文全标题

        Returns:
            该标题对应的论文记录 dict；无命中返回 None。
        """
        return self.match(title)
