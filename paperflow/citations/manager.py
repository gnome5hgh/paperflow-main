"""CitationManager：引用管理编排（bib 真相源 + corpus 易变投影）。

职责：引用解析（全标题/路径 → key+status）、入库（append-only 追加 bib 条目）、
去重、渲染（author-year/numbered/bibtex/gbt7714）、调和（_reconcile：空字段在
渲染视图回填、冲突标记不覆盖——bib 文件绝不被重写）。
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from pathlib import Path

from paperflow.citations import bib as bibmod
from paperflow.citations.bib import BibEntry
from paperflow.citations.corpus import CorpusIndex

#: key 生成的短标题停用词（首词过滤；其余一律保留）
_STOPWORDS = {"the", "a", "an", "of", "in", "for", "and", "on", "to", "with",
              "toward", "towards", "from", "by", "at", "using", "via"}


@dataclass
class ResolvedCitation:
    """引用解析结果：key + 语料状态 + 相关路径。"""

    key: str | None
    status: str                  # "in_corpus" | "missing"
    title: str = ""
    year: str = ""
    note_path: str | None = None
    pdf_path: str | None = None


def _shorttitle(title: str) -> str:
    """从全标题提取首词（过滤停用词/标点、小写）；key 生成用。"""
    words = re.findall(r"[A-Za-z0-9]+", title.lower())
    for w in words:
        if w not in _STOPWORDS:
            return w
    return words[0] if words else "paper"


def gen_key(title: str, authors: str, year: str) -> str:
    """生成 `{firstauthor}{year}{shorttitle}` key（对齐手写库约定）。"""
    first = ""
    if authors:
        first = re.split(r"[,\s]+", authors.strip())[0].lower()
    return f"{first}{year}{_shorttitle(title)}"


def entry_text(key: str, title: str, biblio: dict, external: bool = False) -> str:
    """把书目字段渲染成一条 BibTeX 条目文本（空字段省略，不编造）。"""
    lines = [f"@article{{{key},"]
    if external:
        lines.insert(0, "% EXTERNAL - 库外真实文献（用户确认，非语料库内）")
    ordered = [("author", biblio.get("authors")), ("title", title),
               ("journal", biblio.get("journal")), ("year", biblio.get("year")),
               ("volume", biblio.get("volume")), ("number", biblio.get("number")),
               ("pages", biblio.get("pages"))]
    for name, val in ordered:
        if val:
            lines.append(f"  {name:<7} = {{{val}}},")
    lines.append("}")
    return "\n".join(lines)


class CitationManager:
    """引用管理门面；bib_path 来自 config（非 LLM 可控，无路径注入面）。"""

    def __init__(self, config, rag_service=None, title_extractor=None):
        """构造轻量（不读 bib、不建索引）；重状态首次使用时惰性加载。"""
        self.config = config
        self.bib_path = Path(config.citations_bib_path or
                             Path(config.workspace) / "citations" / "references.bib")
        self._index = CorpusIndex(config, rag_service=rag_service,
                                  title_extractor=title_extractor)
        self._lock = threading.RLock()

    # —— 解析 ——
    def resolve(self, query: str) -> ResolvedCitation:
        """解析：干净全标题 → corpus 精确匹配；路径 → 反查记录。

        干净标题由调用方保证（笔记 H1 / read_pdf 标题 / 用户）；脏输入
        （作者+年份片段）匹配必败 → status="missing"，强制溯源纪律。
        """
        q = (query or "").strip()
        if not q:
            return ResolvedCitation(None, "missing")
        with self._lock:
            self._index.refresh()
            rec = None
            if Path(q).exists():
                rec = self._index.record_by_path(q)
            else:
                rec = self._index.match(q)
            if rec is None:
                return ResolvedCitation(None, "missing")
            title = rec["title"]
            biblio = rec.get("biblio", {})
            # 已有 bib 条目优先用其 key，否则生成（生成后 add_from_pdf 落地）
            existing = bibmod.find_by_title(self.bib_path, title)
            key = existing.key if existing else gen_key(title, biblio.get("authors", ""),
                                                        biblio.get("year", ""))
            return ResolvedCitation(key=key, status="in_corpus", title=title,
                                    year=biblio.get("year", ""),
                                    note_path=rec.get("note_path"),
                                    pdf_path=rec.get("pdf_path"))

    # —— 入库 ——
    def _unique_key(self, key: str) -> str:
        """key 冲突加后缀守卫：同名 key（不同论文）→ 追加数字直至唯一。

        按标题去重已提前早退，走到这里说明是不同论文撞了同一 key；
        gen_key 保持确定性不变，只在落地前保证 key 在库内唯一。
        """
        candidate, n = key, 1
        while self.get(candidate) is not None:
            n += 1
            candidate = f"{key}{n}"
        return candidate

    def add_from_pdf(self, pdf_path: str) -> dict:
        """语料库内 PDF → bib 条目（append-only；已存在则去重返回已有 key）。"""
        with self._lock:
            title, biblio = self._index._pdf_meta(pdf_path)
            if not title:
                return {"key": None, "created": False, "note": "PDF 标题提取失败，未入库"}
            existing = bibmod.find_by_title(self.bib_path, title)
            if existing is not None:
                return {"key": existing.key, "created": False, "note": "已在库"}
            key = self._unique_key(gen_key(title, biblio.get("authors", ""),
                                           biblio.get("year", "")))
            bibmod.append_entry(self.bib_path, entry_text(key, title, biblio))
            return {"key": key, "created": True, "note": "已追加到 references.bib"}

    def add_external(self, title: str, authors: str = "", year: str = "",
                     journal: str = "") -> dict:
        """库外真实文献（用户确认）→ 标 EXTERNAL 的 bib 条目。"""
        with self._lock:
            if not title:
                return {"key": None, "created": False, "note": "缺少标题"}
            existing = bibmod.find_by_title(self.bib_path, title)
            if existing is not None:
                return {"key": existing.key, "created": False, "note": "已在库"}
            key = self._unique_key(gen_key(title, authors, year))
            bibmod.append_entry(self.bib_path, entry_text(
                key, title, {"authors": authors, "year": year, "journal": journal},
                external=True))
            return {"key": key, "created": True, "note": "EXTERNAL 条目已追加"}

    # —— 查询 / 渲染 ——
    def get(self, key: str) -> BibEntry | None:
        """按 key 查条目；无命中返回 None。"""
        for e in bibmod.parse_entries(self.bib_path):
            if e.key == key:
                return e
        return None

    def search(self, q: str) -> list[BibEntry]:
        """模糊搜索（找候选）：标题/作者/key 的子串匹配。"""
        q = q.strip().lower()
        hits = [e for e in bibmod.parse_entries(self.bib_path)
                if not q or q in (e.title + e.authors + e.key).lower()]
        return hits

    def _merged_entry(self, e: BibEntry) -> dict:
        """渲染视图：bib 空字段用当前 PDF biblio 回填（文件不动，真相源稳定）。"""
        fields = dict(e.fields)
        rec = self._index.match(e.title)
        if rec and rec.get("pdf_path"):
            _, biblio = self._index._pdf_meta(rec["pdf_path"])
            for name, key in (("author", "authors"), ("journal", "journal"),
                              ("year", "year"), ("volume", "volume"),
                              ("pages", "pages")):
                if not fields.get(name) and biblio.get(key):
                    fields[name] = biblio[key]
        return fields

    def format(self, keys: list[str], style: str = "author-year") -> str:
        """把 keys 渲染成参考文献段；未知 key 静默跳过。"""
        entries = [e for e in (self.get(k) for k in keys) if e is not None]
        if style == "bibtex":
            return "\n\n".join(e.raw for e in entries)
        if style == "numbered":
            return "\n".join(f"[{i}] {e.authors}. {e.title}. {e.fields.get('journal','')}, "
                             f"{e.fields.get('year','')}." for i, e in enumerate(entries, 1))
        if style == "gbt7714":
            lines = []
            for e in entries:
                f = self._merged_entry(e)
                author = f.get("author", "").split(" and ")[0]
                lines.append(f"{author} 等. {e.title}[J]. {f.get('journal','')}, "
                             f"{f.get('year','')}, {f.get('volume','')}({f.get('number','')}): "
                             f"{f.get('pages','')}.")
            return "\n".join(lines)
        # author-year（默认）
        lines = []
        for e in entries:
            first = e.authors.split(" and ")[0].split(",")[0]
            suffix = " et al." if " and " in e.authors else ""
            lines.append(f"({first}{suffix}, {e.fields.get('year','')}) {e.title}.")
        return "\n".join(lines)

    def _reconcile(self, key: str) -> dict:
        """调和报告：bib 条目 vs 当前 PDF 元数据，空字段回填/冲突标记（不改文件）。

        Returns: {"backfilled": [字段], "conflicts": {字段: (bib值, PDF新值)}}
        """
        e = self.get(key)
        if e is None:
            return {"backfilled": [], "conflicts": {}}
        rec = self._index.match(e.title)
        if not rec or not rec.get("pdf_path"):
            return {"backfilled": [], "conflicts": {}}
        _, biblio = self._index._pdf_meta(rec["pdf_path"])
        backfilled, conflicts = [], {}
        for field, bkey in (("author", "authors"), ("journal", "journal"),
                            ("year", "year"), ("volume", "volume"),
                            ("pages", "pages")):
            new = biblio.get(bkey, "")
            old = e.fields.get(field, "")
            if not new:
                continue
            if not old and new:
                backfilled.append(field)
            elif old and new and old != new:
                conflicts[field] = (old, new)
        return {"backfilled": backfilled, "conflicts": conflicts}
