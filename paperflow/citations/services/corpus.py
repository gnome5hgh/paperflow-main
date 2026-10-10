"""语料标题索引：PDF 标题 + 书目 → 论文中心索引（易变投影）。

corpus_titles.json 是「语料里有哪些论文」的快照，供引用解析做全标题精确
匹配。标题与书目都来自既有实现：标题走 RAG 解析器的轻路径标题出口（元数据 +
首页版面，判据宁空勿错），书目走书目提取器（pdf2bib 联网取权威书目）。索引按
(path, mtime_ns) 增量重建：只对新增/变更的 PDF 重提标题，删除的从索引移除。
索引是易变投影——bib 才是稳定真相源。

**只跟踪 PDF**：溯源的对象是外部论文，笔记是 agent 自己的产物、不参与引用解析。
"""
from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

logger = logging.getLogger(__name__)


class CorpusIndex:
    """论文中心索引：`{norm_title: {title, pdf_path, biblio}}`。

    biblio 仅当取到书目元数据（pdf2bib 联网取）时存在。

    Attributes:
        config: PaperFlowConfig，语料目录来源
        _meta: PaperMetaExtractor | None，惰性获取（pdf2bib 书目提取）
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
        """惰性获取书目提取器（首次用才构造，避免无谓拉起取数依赖）。

        首选 pdf2bib 取数（不需要模型）；模型只在 pdf2bib 取不到时作首页兜底，
        所以这里顺手把 LLM 客户端备好。LLM 未配置（无 api_key）时 LLMClient 构造即
        抛——这不该拖垮整条取数链，捕获后按「无兜底」继续（pdf2bib 那一级照常工作）。

        Returns:
            PaperMetaExtractor: 书目提取器实例。
        """
        if self._meta_extractor is None:
            from paperflow.citations.parsers import PaperMetaExtractor
            llm = None
            try:
                from paperflow.core.llm import LLMClient
                llm = LLMClient(self.config.llm)
            except Exception as e:
                logger.warning("LLM 未配置，书目提取无首页兜底：%s", e)
            self._meta_extractor = PaperMetaExtractor(llm=llm)
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

        先读回上次的磁盘缓存再扫描，否则进程每次启动都会把每一篇 PDF 重新解析一遍
        标题、并重跑一次书目提取（联网取标识符 + 权威书目，整库会拖上几分钟）。
        """
        with self._lock:
            self.ensure_loaded()

            # 1. 遍历论文目录，收集当前所有 PDF 的绝对路径与 mtime。
            current: dict[str, int] = {}
            root = self.config.corpus.pdf_dir
            if root:
                for p in Path(root).rglob("*.pdf"):
                    st = p.stat()
                    path = str(p)
                    current[path] = st.st_mtime_ns

                    # 2. 新增或 mtime 变了才重提标题；未变更的沿用缓存。
                    if self._mtime.get(path) != st.st_mtime_ns:
                        self._upsert(path)

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

    def _upsert(self, path: str) -> None:
        """更新或插入一条 PDF 记录：提标题 + 书目，写入或合并到 _records。

        关键算法/边界处理（Ghost Record 防御）：
            文件路径和标题都可能发生变化。若某个 PDF 之前属于标题 A，现在标题变为 B，
            在写 B 的记录前必须先把该路径从标题 A 的记录里解绑，否则同一路径会同时
            出现在两条记录中，形成幽灵记录。

        Args:
            path: PDF 文件绝对路径（字符串）。
        """
        title, biblio = self._pdf_meta(path)
        # 提不出标题就不索引（避免损坏文件或空标题污染索引）
        if not title:
            return
        norm = self.normalize(title)

        # 幽灵记录清理：把该路径从其他记录里解绑
        for other in self._records.values():
            if other.get("pdf_path") == path:
                other["pdf_path"] = None

        rec = self._records.setdefault(norm, {"title": title, "pdf_path": None,
                                              "biblio": {}})
        rec["title"] = title    # 标题也可能被修正，更新为最新值
        rec["pdf_path"] = path
        if biblio:
            rec["biblio"] = biblio

    def _remove(self, path: str) -> None:
        """从所有包含该路径的记录里摘除引用；记录空壳则删除。

        Args:
            path: 需要移除的文件路径。
        """
        for norm, rec in list(self._records.items()):
            if rec.get("pdf_path") == path:
                rec["pdf_path"] = None
            # 记录不再指向任何 PDF 时移除（避免空壳占用归一化标题 key）
            if not rec.get("pdf_path"):
                del self._records[norm]

    def _pdf_meta(self, path: str) -> tuple[str, dict]:
        """取一篇 PDF 的标题与书目元数据。

        **触发点必须在这里（建立语料索引时）**，不能推后到「入库登记那一刻」：
        引用解析先算**预备 key**、入库时算**正式 key**，两者都调
        ``gen_key(title, authors, year)`` 且都读同一份记录里的 biblio。把取数推后，
        预备 key 会退化成短标题形态、与正式 key 对不上，产物里的 `[来源:key§节]`
        就失效了。结果随磁盘缓存按 mtime 增量，所以只在文件新增/变更时才付这次
        调用（书目那条会联网取标识符与权威书目）。

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

    # —— 查询 ——
    def pdf_records(self) -> list[dict]:
        """返回带 PDF 的论文记录（批量入库用）；调用方负责先 refresh()。

        Returns:
            记录 dict 的副本列表，每条含 title/pdf_path/biblio。
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
        """按 PDF 路径反查论文记录（source path 兜底入口）。

        Args:
            path: str，PDF 文件路径

        Returns:
            命中该路径的论文记录 dict；无命中返回 None。
        """
        p = str(Path(path).resolve())
        for rec in self._records.values():
            if rec.get("pdf_path") == p:
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
