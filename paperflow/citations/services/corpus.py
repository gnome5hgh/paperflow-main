"""语料标题索引：PDF 解析标题 → 论文中心索引（易变投影）。

corpus_titles.json 是「语料里有哪些论文」的快照，供引用解析做全标题精确
匹配。标题与书目都来自既有实现：标题走 RAG 解析器的轻路径标题出口（元数据 +
首页版面，判据宁空勿错），书目走首页书目提取器（一次 LLM 调用）。索引按
(path, mtime_ns) 增量重建：只对新增/变更文件重提标题，删除的文件从索引移除。
索引是易变投影——bib 才是稳定真相源。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path


class CorpusIndex:
    """论文中心索引：`{norm_title: {title, note_path, pdf_path, biblio}}`。

    一篇论文可同时有笔记与 PDF（同全标题合并成一条记录）。biblio 仅当 PDF
    解析出书目元数据时存在；笔记只贡献 note_path + title。

    Attributes:
        config: PaperFlowConfig，语料目录来源
        _meta: PaperMetaExtractor | None，惰性获取（首页书目提取）
        _lock: threading.RLock，保护索引重建与查询
        _cache_path: Path，语料标题缓存文件（workspace/citations/corpus_titles.json）
        _records: dict[str, dict]，归一化标题 → 论文记录
        _mtime: dict[str, int]，文件路径 → mtime_ns（增量重建判据）
        _loaded: bool，是否已尝试读回磁盘缓存（首次 refresh 读一次，之后不再读）
    """

    def __init__(self, config, meta_extractor=None):
        """注入 config；书目提取器可注入桩（测试），缺省惰性获取。

        Args:
            config: PaperFlowConfig，配置来源
            meta_extractor: PaperMetaExtractor | None，可注入桩（缺省惰性获取）
        """
        self.config = config
        self._meta_extractor = meta_extractor
        self._lock = threading.RLock()
        self._cache_path = Path(config.runtime.workspace) / "citations" / "corpus_titles.json"
        self._records: dict[str, dict] = {}   # norm_title -> 论文记录
        self._mtime: dict[str, int] = {}      # path -> mtime_ns（增量判据）
        self._loaded = False                  # 磁盘缓存只读回一次

    # —— 惰性依赖（首次访问才构造重组件）——
    def _meta(self):
        """惰性获取首页书目提取器（要动 LLM 才构造，只在真需要书目时才付这个代价）。

        Returns:
            PaperMetaExtractor: 书目提取器实例。
        """
        if self._meta_extractor is None:
            from paperflow.citations.parsers import PaperMetaExtractor
            from paperflow.core.llm import LLMClient
            self._meta_extractor = PaperMetaExtractor(
                LLMClient(self.config.llm))
        return self._meta_extractor

    @staticmethod
    def normalize(title: str) -> str:
        """标题归一化（与 storage/bib.py 的 _normalize 同规则：小写+去标点+折叠空白）。

        Args:
            title: str，待归一化标题

        Returns:
            与 storage/bib.py 的 _normalize 同规则的小写去标点折叠空白串。
        """
        import re
        return re.sub(r"[\s\W_]+", "", title.lower())

    # —— 索引生命周期 ——
    def refresh(self) -> None:
        """增量重建：扫描语料库目录，只对新增/变更文件重提标题，删除的移除。

        先读回上次的磁盘缓存再扫描，否则进程每次启动都会把每一篇 PDF 重新读一遍
        首页、并重跑一次书目提取的模型调用。
        """
        with self._lock:
            self.ensure_loaded()

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

            # 4. 更新 _mtime 为当前快照；快照没变就不写盘——引用解析每次都调 refresh，
            # 语料没动就不该反复重写这个文件。比对快照而不是逐条标记变更：提不出标题的
            # 文件（损坏 PDF）也算改动，它的 mtime 得存下来，否则每次启动都要重试解析。
            changed = current != self._mtime
            self._mtime = current
            if changed or not self._cache_path.exists():
                self.save()

    def ensure_loaded(self) -> None:
        """确保磁盘缓存已读回（幂等；refresh 内部调用，接入方不必手动调）。"""
        if self._loaded:
            return
        self._loaded = True
        self.load()

    def load(self) -> None:
        """从磁盘载入缓存；缺失或损坏时保持空状态（按冷启动重建，不抛异常）。

        缓存文件是随时可重建的投影，读它不该把调用方拖下水：文件被写坏一半
        （进程中断、手工编辑）时下一次查引用必须照常工作，代价只是重扫一遍。
        """
        try:
            data = json.loads(self._cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if not isinstance(data, dict):
            return
        records, mtime = data.get("records"), data.get("mtime")
        if not isinstance(records, dict) or not isinstance(mtime, dict):
            return
        self._records = records
        try:
            self._mtime = {k: int(v) for k, v in mtime.items()}
        except (TypeError, ValueError):
            # mtime 表读不出来就只丢它——记录仍可用，下次扫描按「全部已变更」重提
            self._mtime = {}

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
        """取一篇 PDF 的标题与书目元数据。

        **触发点必须在这里（建立语料索引时）**，不能推后到「入库登记那一刻」：
        引用解析先算**预备 key**、入库时算**正式 key**，两者都调
        ``gen_key(title, authors, year)`` 且都读同一份记录里的 biblio。把取数推后，
        预备 key 会退化成短标题形态、与正式 key 对不上，产物里的 `[来源:key§节]`
        就失效了。结果随磁盘缓存按 mtime 增量，所以只在文件新增/变更时才付这次
        调用；冷启动成本与「整库交给外部解析服务」相比只低不高。

        两路都自带降级：标题取不到、书目取不到都不抛——标题为空即不索引该 PDF
        （调用方的既有分支），书目为空则退化为缺字段的引用条目。

        Args:
            path: str，PDF 文件路径。

        Returns:
            tuple[str, dict]: (标题, 书目字段字典)。标题拿不准时为空串。
        """
        try:
            from paperflow.rag.parsers.pdf_extract import pdf_title
            title = pdf_title(path)
        except Exception:
            # 读不动（损坏/加密/不存在）等同「没标题」——不索引该 PDF
            title = ""
        try:
            biblio = self._meta().from_pdf(path).as_dict()
        except Exception:
            # 书目是加分项，取不到不影响标题与索引
            biblio = {}
        return title, biblio

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
