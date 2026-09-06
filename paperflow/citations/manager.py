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
#: 例如 "The Attention Mechanism" → "attention"（过滤 "the"）。
_STOPWORDS = {"the", "a", "an", "of", "in", "for", "and", "on", "to", "with",
              "toward", "towards", "from", "by", "at", "using", "via"}


@dataclass
class ResolvedCitation:
    """引用解析结果：key + 语料状态 + 相关路径。

    Attributes:
        key: BibTeX 条目的键，若 status="missing" 则为 None。
        status: "in_corpus"（语料库内）或 "missing"（库外/未找到）。
        title: 论文全标题。
        year: 发表年份。
        note_path: 关联的笔记文件路径（若有）。
        pdf_path: 关联的 PDF 文件路径（若有）。
    """

    key: str | None
    status: str                  # "in_corpus" | "missing"
    title: str = ""
    year: str = ""
    note_path: str | None = None
    pdf_path: str | None = None
    #: key 是否已落地在 references.bib（bib 真相源）。in_corpus 只说明语料标题
    #: 索引命中——key 可能是现场生成、尚未入库的（真实使用测试 P1-5 的溯源链
    #: 断裂根因）。in_bib=False 时标注 [来源:key§节] 属于无据声称，必须先
    #: add_citation 成功或降级为 [⚠未入库]。
    in_bib: bool = False


def _shorttitle(title: str) -> str:
    """从全标题提取用于生成 key 的短标题（首个非停用词）。

    算法：
        1. 用正则提取所有字母数字词（过滤标点）。
        2. 返回第一个不在 _STOPWORDS 中的词。
        3. 若所有词均为停用词，则回退返回第一个词；若无词则返回 "paper"。

    边界：标题 "A Study of ..." → 停用词 "a" 被过滤，返回 "study"。
    """
    words = re.findall(r"[A-Za-z0-9]+", title.lower())
    for w in words:
        if w not in _STOPWORDS:
            return w
    return words[0] if words else "paper"


def gen_key(title: str, authors: str, year: str) -> str:
    """生成 `{firstauthor}{year}{shorttitle}` BibTeX key。

    对齐手写库约定，确保人类可读且稳定。
    例如：作者 "John Smith"、年份 "2024"、标题 "Attention Is All You Need"
          → "smith2024attention"（忽略 "is/all/you/need" 等停用词）。

    Args:
        title: 论文全标题。
        authors: 作者字符串（如 "Smith, John and Doe, Jane"），
                 只取第一个作者（按逗号或空格分割）。
        year: 发表年份（字符串）。

    Returns:
        生成的 key（小写）。
    """
    first = ""
    if authors:
        first = re.split(r"[,\s]+", authors.strip())[0].lower()
    return f"{first}{year}{_shorttitle(title)}"


def entry_text(key: str, title: str, biblio: dict, external: bool = False) -> str:
    """把书目字段渲染成一条 BibTeX 条目文本（用于追加到 .bib 文件）

    空字段自动省略，不编造，避免写入无意义的占位符。字段顺序固定为：
    author → title → journal → year → volume → number → pages。

    Args:
        key: BibTeX key。
        title: 论文标题。
        biblio: 书目元数据字典，键如 "authors"、"journal"、"year" 等。
        external: 若为 True，在条目上方添加 `% EXTERNAL` 注释行。

    Returns:
        完整的 BibTeX 条目字符串（含换行）。
    """
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
        """构造轻量（不读 bib、不建索引）；重状态首次使用时惰性加载。

        Args:
            config: 应用配置对象，需包含 workspace、citations_bib_path 等。
            rag_service: 可注入的 RAG 服务实例（测试用），缺省惰性获取。
            title_extractor: 可注入的标题提取器（测试用），缺省惰性获取。
        """
        self.config = config
        # bib 路径：优先使用 config 指定，否则 fallback 到 workspace/citations/references.bib
        self.bib_path = Path(config.citations_bib_path or
                             Path(config.workspace) / "citations" / "references.bib")
        self._index = CorpusIndex(config, rag_service=rag_service,
                                  title_extractor=title_extractor)
        self._lock = threading.RLock()

    # —— 解析 ——
    def resolve(self, query: str) -> ResolvedCitation:
        """解析查询字符串（全标题或文件路径）为引用结果。

        干净标题由调用方保证（笔记 H1 / read_pdf 标题 / 用户）；脏输入
        （作者+年份片段）匹配必败 → status="missing"，强制溯源纪律。

        解析策略（二义性检测）：
            1. 若 query 是已存在的文件路径 → 调用 record_by_path 反查记录。
            2. 否则将其视为论文全标题 → 调用 corpus 索引做归一化精确匹配。
            3. 注：调用方必须保证传入的是干净的全标题或路径（来自笔记 H1 或 PDF 元数据）。
               若传入 "Smith 2024" 这类片段，匹配必败，返回 status="missing"，
               以此强制引用溯源纪律（先有语料库实体，再生成引用）。

        Returns:
            ResolvedCitation 对象。若命中，优先使用 bib 库中已存在的 key（若标题相同），
            否则按 gen_key 生成新 key（后续通过 add_from_pdf 落地）。
        """
        q = (query or "").strip()
        if not q:
            return ResolvedCitation(None, "missing")
        with self._lock:
            # 刷新 corpus 索引，确保文件系统变更被感知
            self._index.refresh()
            rec = None

            # 路径还是标题？以文件系统是否存在为判据
            if Path(q).exists():
                rec = self._index.record_by_path(q)
            else:
                rec = self._index.match(q)
            if rec is None:
                return ResolvedCitation(None, "missing")
            title = rec["title"]
            biblio = rec.get("biblio", {})
            # 若 bib 库中已存在同标题条目，直接沿用其 key，避免重复生成；
            # 否则现场生成 key——此时 in_bib=False，lookup_citation 会给出强指令
            existing = bibmod.find_by_title(self.bib_path, title)
            key = existing.key if existing else gen_key(title, biblio.get("authors", ""),
                                                        biblio.get("year", ""))
            return ResolvedCitation(key=key, status="in_corpus", title=title,
                                    year=biblio.get("year", ""),
                                    note_path=rec.get("note_path"),
                                    pdf_path=rec.get("pdf_path"),
                                    in_bib=existing is not None)

    # —— 入库 ——
    def _unique_key(self, key: str) -> str:
        """key 冲突加后缀守卫：同名 key（不同论文）→ 追加数字直至唯一。

        调用时机：已在标题去重中提前返回（同标题不会走到这里），
        所以此处冲突意味着不同论文生成了相同的 key（极少见，但需防御）。
        gen_key 保持确定性，因此只能在落地时动态调整。

        边界：若 key 已存在，尝试 key2、key3... 直到找到空位。
        """
        candidate, n = key, 1
        while self.get(candidate) is not None:
            n += 1
            candidate = f"{key}{n}"
        return candidate

    def add_from_pdf(self, pdf_path: str) -> dict:
        """语料库内 PDF → bib 条目（append-only；已存在则去重返回已有 key）。

        关键防御（宁缺毋滥原则）：
            若 PDF 元数据缺少作者或年份，则**拒绝入库**并返回错误提示。
            这是为了防止写出 `@article{key, title={…}}` 这种空壳条目——
            缺少作者/年份的条目无法支撑任何有意义的引用格式（author-year 或 gbt7714 都依赖它们）。
            用户应启动 GROBID 服务或手动补充元数据后再试。

        Returns:
            dict 包含以下字段：
                - key: BibTeX key（若成功或已存在）；失败时为 None。
                - created: bool，是否新创建了条目。
                - note: 状态/提示信息。
        """
        with self._lock:
            title, biblio = self._index._pdf_meta(pdf_path)
            if not title:
                return {"key": None, "created": False, "note": "PDF 标题提取失败，未入库"}
            # 严格校验：没有作者或年份的条目是废条目，拒绝写入
            if not biblio.get("authors") or not biblio.get("year"):
                # 拒绝信息必须可行动（真实使用测试 P1-5：模型忽略了单纯一句 note）：
                # 给出三条明确出路，任选其一，不允许带着「已确认」的假引用继续
                return {"key": None, "created": False,
                        "note": "PDF 元数据不足（缺作者/年份），拒绝入库。三选一："
                                "① 启动 GROBID 后重试（解析作者/年份）；"
                                "② 用 add_external 手动提供作者/年份入库（需用户确认字段）；"
                                "③ 笔记中该引用降级标注为 [⚠未入库]，不得写「经 lookup_citation 确认」。"}
            # 标题去重：若已存在同标题条目，直接返回已有 key（不重复追加）
            existing = bibmod.find_by_title(self.bib_path, title)
            if existing is not None:
                return {"key": existing.key, "created": False, "note": "已在库"}
            key = self._unique_key(gen_key(title, biblio.get("authors", ""),
                                           biblio.get("year", "")))
            bibmod.append_entry(self.bib_path, entry_text(key, title, biblio))
            return {"key": key, "created": True, "note": "已追加到 references.bib"}

    def add_external(self, title: str, authors: str = "", year: str = "",
                     journal: str = "") -> dict:
        """库外真实文献（用户确认）→ 标 EXTERNAL 的 bib 条目。

        区别于 add_from_pdf：不依赖 PDF 文件，仅凭用户提供的字段入库。
        条目上方会添加 `% EXTERNAL` 注释，便于与语料库内文献区分。

        Returns:
            同 add_from_pdf 的返回结构。
        """
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
        """模糊搜索（找候选）：标题/作者/key 的子串匹配。

        用于交互式候选选择（如用户输入 "attention" 列出所有相关条目）。
        """
        q = q.strip().lower()
        hits = [e for e in bibmod.parse_entries(self.bib_path)
                if not q or q in (e.title + e.authors + e.key).lower()]
        return hits

    def _merged_entry(self, e: BibEntry) -> dict:
        """渲染视图：bib 空字段用当前 PDF biblio 回填（文件不动，真相源稳定）。

        设计原则（真相源稳定）：
            references.bib 是权威真相源，绝不重写。但 PDF 文件可能在入库后更新了元数据
            （如 GROBID 重新解析后获得了更全的期刊/页码信息）。
            `_merged_entry` 在不触碰 bib 文件的前提下，将 bib 中缺失的字段
            （author/journal/year/volume/pages）用 PDF 最新元数据补齐，供渲染使用。

        冲突策略：仅当 bib 字段为空（`not fields.get(name)`）时才回填新值；
                   若两者都有且不同，以 bib 为准（不覆盖）。
        """
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
        """将给定的 keys 列表渲染为参考文献字符串。

        支持四种样式：
            - "bibtex": 返回原始 BibTeX 条目原文（`raw` 字段）。
            - "numbered": 编号列表 `[1] Author. Title. Journal, Year.`
            - "gbt7714": 国标格式 `Author 等. Title[J]. Journal, Year, Vol(Iss): Pages.`
            - "author-year": (默认) `(Author et al., Year) Title.`

        未知 key 会被静默跳过（不阻塞渲染），适用于部分引用未入库的草稿场景。
        """
        entries = [e for e in (self.get(k) for k in keys) if e is not None]
        if style == "bibtex":
            return "\n\n".join(e.raw for e in entries)
        if style == "numbered":
            return "\n".join(f"[{i}] {e.authors}. {e.title}. {e.fields.get('journal','')}, "
                             f"{e.fields.get('year','')}." for i, e in enumerate(entries, 1))
        if style == "gbt7714":
            lines = []
            for e in entries:
                # gbt7714 需要完整字段，用 _merged_entry 回填缺失信息
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
        """调和报告：比较 bib 条目与当前 PDF 元数据的差异，空字段回填/冲突标记（不改文件）。

        返回信息分为两类：
            1. backfilled: 列表，bib 中为空但 PDF 中有值的字段（渲染时会被回填）。
            2. conflicts: 字典，两者都有值但不一致的字段，值为 `(bib值, PDF新值)`。

        用途：供用户或上层 agent 审查，决定是否接受回填或手动更新 bib 文件。
        注意：此方法**只读**，绝不修改 bib 文件。

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
