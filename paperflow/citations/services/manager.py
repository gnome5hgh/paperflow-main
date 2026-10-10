"""CitationManager：引用管理编排（bib 真相源 + corpus 易变投影）。

职责：引用解析（全标题/路径 → key+status）、入库（append-only 追加 bib 条目）、
去重、渲染（author-year/numbered/bibtex/gbt7714）、调和（_reconcile：空字段在
渲染视图回填、冲突标记不覆盖——bib 文件绝不被重写）。

文件读写原语在 storage/、key 生成规则在 services/keys.py、数据模型在 schemas/，
本模块只做编排——它不碰正则、不拼 BibTeX 文本。
"""
from __future__ import annotations

import threading
from pathlib import Path

from paperflow.citations.constants import CitationStatus, RemoveOutcome
from paperflow.citations.schemas import BibEntry, ResolvedCitation
from paperflow.citations.services.corpus import CorpusIndex
from paperflow.citations.services.keys import gen_key
from paperflow.citations.storage import bib as bibmod

class CitationManager:
    """引用管理门面；bib_path 来自 config（非 LLM 可控，无路径注入面）。

    Attributes:
        config: PaperFlowConfig，配置来源
        bib_path: Path，references.bib 路径（config 指定，否则回退 workspace/citations/）
        _index: CorpusIndex，语料标题索引（论文中心快照）
        _lock: threading.RLock，保证「刷新索引 → 查 corpus → 查 bib」为原子快照
    """

    def __init__(self, config, meta_extractor=None):
        """构造轻量（不读 bib、不建索引）；重状态首次使用时惰性加载。

        Args:
            config: 应用配置对象，需包含 workspace、citations_bib_path 等。
            meta_extractor: 可注入的书目提取器（测试用），缺省惰性获取。
        """
        self.config = config
        # bib 路径：优先使用 config 指定，否则 fallback 到 workspace/citations/references.bib
        self.bib_path = Path(config.corpus.citations_bib_path or
                             Path(config.runtime.workspace) / "citations" / "references.bib")
        self._index = CorpusIndex(config, meta_extractor=meta_extractor)
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

        Args:
            query: str，全标题或现存文件路径（脏输入按缺失处理）
        """
        q = (query or "").strip()
        # 空输入守卫：零开销早退（不进锁、不触发索引刷新），空查询无溯源意义
        if not q:
            return ResolvedCitation(None, CitationStatus.MISSING)
        # 全程持锁：保证「刷新索引 → 查 corpus → 查 bib」是原子快照——
        # 若中途释放，并发下 corpus 投影与 bib 真相源可能错位（如标题刚入库）
        with self._lock:
            # 第1步 刷新语料索引（点名册）：resolve 是引用链唯一查询入口，
            # 进程外用户可能刚增删过笔记/PDF；refresh 按 mtime 增量重建，
            # 未变更文件零重提（见 CorpusIndex.refresh），代价可接受
            self._index.refresh()
            rec = None

            # 第2步 判定 query 是路径还是标题，判据 = 文件系统是否存在：语料内文件路径恒为绝对路径（entities.py 提取即绝对路径约定），
            # 论文标题不会恰好撞上一个现存路径，二者互斥、无二义。
            # 路径 → record_by_path 反查
            if Path(q).exists():
                rec = self._index.record_by_path(q)
            # 标题 → 直接从 corpus 中 match 归一化精确匹配
            else:
                rec = self._index.match(q)

            # 第3步 未命中 → missing（溯源纪律的落点）：语料里没有这篇论文就没有可溯源的实体，
            # 此处绝不返回 key——调用方（lookup_citation工具）据此提示标 [⚠无支撑]，而不是拿着编造的 key 继续
            if rec is None:
                return ResolvedCitation(None, CitationStatus.MISSING)

            # 第4步 命中 → 组装返回值。关键：两层事实分开查证——
            # corpus 命中只证明「语料里有这篇」（status=in_corpus）；
            # 「bib 里登记没登记」（in_bib）是独立事实，把前者当成后者会把「语料命中」误当"引用已确认"
            title = rec["title"]
            biblio = rec.get("biblio", {})
            # bib 户口本按标题查重：已有条目 → 沿用其 key（同一论文不重复登记）；
            # 没有 → 按 gen_key 规则现场生成预备 key。注意生成的 key 只是"预定编号"，尚未写入 references.bib——
            # 调用方须先 add_citation 落地才能标 [来源:key§节]，落地前属无据声称（lookup_citation 强提示）
            existing = bibmod.find_by_title(self.bib_path, title)
            key = existing.key if existing else gen_key(title, biblio.get("authors", ""),
                                                        biblio.get("year", ""))
            # status 恒为 "in_corpus"（走到这里必已命中）；in_bib 区分"已在库"与"现场生成待落地"，lookup_citation 据此分级提示
            return ResolvedCitation(key=key, status=CitationStatus.IN_CORPUS, title=title,
                                    year=biblio.get("year", ""),
                                    pdf_path=rec.get("pdf_path"),
                                    in_bib=existing is not None)

    # —— 入库 ——
    def _unique_key(self, key: str) -> str:
        """key 冲突加后缀守卫：同名 key（不同论文）→ 追加数字直至唯一。

        调用时机：已在标题去重中提前返回（同标题不会走到这里），
        所以此处冲突意味着不同论文生成了相同的 key（极少见，但需防御）。
        gen_key 保持确定性，因此只能在落地时动态调整。

        边界：若 key 已存在，尝试 key2、key3... 直到找到空位。

        Args:
            key: str，按规则生成的预备 key

        Returns:
            与库中现有 key 不冲突的 key（冲突则追加数字直至唯一）。
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
            用户可用 read_pdf 读出首页的作者/年份，或手动补充元数据后再试。

        Returns:
            dict 包含以下字段：
                - key: BibTeX key（若成功或已存在）；失败时为 None。
                - created: bool，是否新创建了条目。
                - note: 状态/提示信息。

        Args:
            pdf_path: str，语料库内 PDF 路径
        """
        with self._lock:
            title, biblio = self._index._pdf_meta(pdf_path)
            if not title:
                return {"key": None, "created": False, "note": "PDF 标题提取失败，未入库"}
            # 严格校验：没有作者或年份的条目是废条目，拒绝写入
            if not biblio.get("authors") or not biblio.get("year"):
                # 拒绝信息必须可行动（单纯一句 note 会被模型忽略）：
                # 给出三条明确出路，任选其一，不允许带着「已确认」的假引用继续
                return {"key": None, "created": False,
                        "note": "PDF 元数据不足（缺作者/年份），拒绝入库。三选一："
                                "① 用 read_pdf 读 PDF 首页自己读出作者与年份，再重试；"
                                "② 用 add_external 手动提供作者/年份入库（需用户确认字段）；"
                                "③ 笔记中该引用降级标注为 [⚠未入库]，不得写「经 lookup_citation 确认」。"}
            # 标题去重：若已存在同标题条目，直接返回已有 key（不重复追加）
            existing = bibmod.find_by_title(self.bib_path, title)
            if existing is not None:
                return {"key": existing.key, "created": False, "note": "已在库"}
            key = self._unique_key(gen_key(title, biblio.get("authors", ""),
                                           biblio.get("year", "")))
            bibmod.append_entry(self.bib_path, bibmod.entry_text(key, title, biblio))
            return {"key": key, "created": True, "note": "已追加到 references.bib"}

    def add_external(self, title: str, authors: str = "", year: str = "",
                     journal: str = "") -> dict:
        """库外真实文献（用户确认）→ 标 EXTERNAL 的 bib 条目。

        区别于 add_from_pdf：不依赖 PDF 文件，仅凭用户提供的字段入库。
        条目上方会添加 `% EXTERNAL` 注释，便于与语料库内文献区分。

        Returns:
            同 add_from_pdf 的返回结构。

        Args:
            title: str，标题
            authors: str，作者
            year: str，年份
            journal: str，期刊（可选）
        """
        with self._lock:
            if not title:
                return {"key": None, "created": False, "note": "缺少标题"}
            existing = bibmod.find_by_title(self.bib_path, title)
            if existing is not None:
                return {"key": existing.key, "created": False, "note": "已在库"}
            key = self._unique_key(gen_key(title, authors, year))
            bibmod.append_entry(self.bib_path, bibmod.entry_text(
                key, title, {"authors": authors, "year": year, "journal": journal},
                external=True))
            return {"key": key, "created": True, "note": "EXTERNAL 条目已追加"}

    def remove(self, key_or_title: str) -> dict:
        """删除一条引用（key 精确命中或标题归一化命中）。

        删除是高危操作：多条命中时不猜——返回 ambiguous 与候选列表，由
        工具层转述给用户选择后重调；只有唯一命中才真正删除。

        Args:
            key_or_title: bib key 或论文标题。

        Returns:
            dict: status ∈ RemoveOutcome；removed 时带 key；
            ambiguous 时带 candidates（key+title 列表）。
        """
        with self._lock:
            q = key_or_title.strip()
            entries = bibmod.parse_entries(self.bib_path)
            exact = [e for e in entries if e.key == q]
            if not exact:
                exact = bibmod.find_all_by_title(self.bib_path, q)
            if not exact:
                return {"status": RemoveOutcome.NOT_FOUND, "key": None, "candidates": []}
            if len(exact) > 1:
                return {"status": RemoveOutcome.AMBIGUOUS, "key": None,
                        "candidates": [{"key": e.key, "title": e.title}
                                       for e in exact]}
            removed = bibmod.remove_entries(self.bib_path, {exact[0].key})
            status = RemoveOutcome.REMOVED if removed else RemoveOutcome.NOT_FOUND
            return {"status": status, "key": exact[0].key if removed else None,
                    "candidates": []}

    def sync_all(self) -> dict:
        """语料库全量同步入 bib：逐条按 add_from_pdf 语义入库，幂等增量。

        枚举源是语料标题索引（CorpusIndex，论文中心快照）：只对带 pdf_path
        的记录入库，纯笔记记录天然跳过。已在库的条目按标题去重跳过，因此
        重复调用安全；缺作者/年份的记录沿用宁缺毋滥防御拒绝入库，在
        rejected 中逐条列出（补齐作者/年份后重跑即可）。

        Returns:
            dict: total=带 PDF 的记录数；added=新入库 key；skipped=已在库 key；
            rejected=[{pdf_path, note}] 拒绝入库明细。
        """
        self._index.refresh()
        records = self._index.pdf_records()
        added, skipped, rejected = [], [], []
        for rec in records:
            r = self.add_from_pdf(rec["pdf_path"])
            if r["created"]:
                added.append(r["key"])
            elif r["key"]:
                skipped.append(r["key"])
            else:
                rejected.append({"pdf_path": rec["pdf_path"], "note": r["note"]})
        return {"total": len(records), "added": added,
                "skipped": skipped, "rejected": rejected}

    # —— 查询 / 渲染 ——
    def get(self, key: str) -> BibEntry | None:
        """按 key 查条目；无命中返回 None。

        Args:
            key: str，站点引用键

        Returns:
            命中的 BibEntry；无命中返回 None。
        """
        for e in bibmod.parse_entries(self.bib_path):
            if e.key == key:
                return e
        return None

    def search(self, q: str) -> list[BibEntry]:
        """模糊搜索（找候选）：标题/作者/key 的子串匹配。

        用于交互式候选选择（如用户输入 "attention" 列出所有相关条目）。

        Args:
            q: str，搜索词（标题/作者/key 的子串）

        Returns:
            命中的 BibEntry 列表（空串返回全部）。
        """
        q = q.strip().lower()
        hits = [e for e in bibmod.parse_entries(self.bib_path)
                if not q or q in (e.title + e.authors + e.key).lower()]
        return hits

    def _merged_entry(self, e: BibEntry) -> dict:
        """渲染视图：bib 空字段用当前 PDF biblio 回填（文件不动，真相源稳定）。

        设计原则（真相源稳定）：
            references.bib 是权威真相源，绝不重写。但 PDF 文件可能在入库后更新了元数据
            （如书目重新提取后获得了更全的期刊信息）。
            `_merged_entry` 在不触碰 bib 文件的前提下，将 bib 中缺失的字段
            （author/journal/year/volume/pages）用 PDF 最新元数据补齐，供渲染使用。

        冲突策略：仅当 bib 字段为空（`not fields.get(name)`）时才回填新值；
                   若两者都有且不同，以 bib 为准（不覆盖）。

        Args:
            e: BibEntry，bib 条目

        Returns:
            渲染用字段 dict：bib 空字段用当前 PDF 元数据回填，冲突以 bib 为准（不改文件）。
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

        Args:
            keys: list[str]，待渲染的引用键（未知 key 静默跳过）
            style: str，渲染样式：bibtex / numbered / gbt7714 / author-year（默认）

        Returns:
            渲染后的参考文献字符串。
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

        Args:
            key: str，站点引用键
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
