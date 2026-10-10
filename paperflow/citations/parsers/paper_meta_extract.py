"""书目元数据提取：先经 pdf2bib 取权威书目，取不到再用模型读首页兜底。

**只做这一件事**——标题不从这里取（标题是解析器的一层产出，见
`rag/parsers/pdf_extract.pdf_title`）。书目元数据只有引用域消费（语料索引是唯一调用点），
所以代码归引用域。

**两级取数**：
1. **pdf2bib（首选）**：先在本地从 PDF 元数据、文件名、正文里找出 DOI 或 arXiv
   标识符，再拿标识符去公开档案换回作者 / 年份 / 期刊。数据来自出版方登记，最可信。
   需要联网；找不到标识符、联网失败或取数超时都会落到第二级。
2. **模型读首页（兜底）**：pdf2bib 拿不到时，把首页文本交给 `StructuredOutput` 让
   模型读出三项。模型可能读不到、也可能读错，所以只作兜底——它拿不到就如实留空。
   未配置 LLM 时这一级直接不可用（返回空），不影响第一级。

**副作用已在配置里关掉**：pdf2bib 默认会把找到的标识符写回 PDF 元数据，并打印逐步骤
日志。这里在首次调用前统一关掉写回与日志，绝不改动语料文件。
"""
import asyncio
import logging
import threading
from dataclasses import dataclass

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: pdf2bib 单篇取数的超时（秒）。pdf2bib 内部的 HTTP 请求没有超时设置，网络半开
#: （能连上但不响应）时会一直挂住；语料刷新持着引用管理的锁，挂住会拖垮整个
#: 引用域，所以这里兜一个上限，超时按「取不到」降级。
_CALL_TIMEOUT_SECONDS = 60.0

#: 送进模型的首页文本上限（字符）：作者、年份、期刊都印在首页最上面一段，
#: 取多了只会把摘要正文也塞进去，白烧 token。
FIRST_PAGE_CHARS = 2000

_PROMPT = (
    "从下面的论文首页文本里读出版权信息中的书目元数据。只读文本里真的写了的内容，"
    "任何一项读不到就留空字符串——**不要推测、不要用常识补全**，宁可空着。\n"
    "输出 JSON：{{authors, year, journal}}。\n"
    "authors 用 BibTeX 形式给作者（每位写成「姓, 名」，多位之间用 and 连接，"
    "例如 `Zhang, Alice and Li, Bob`，不要用逗号分隔全名）；year 给四位年份；"
    "journal 给期刊或会议名。\n"
    "首页文本：\n{text}"
)

_config_lock = threading.Lock()
_configured = False


def _ensure_configured() -> None:
    """首次调用前配置 pdf2bib / pdf2doi：关掉元数据写回、日志与 MuPDF 报错。

    三项都是进程级全局设置，只会生效一次。关写回是硬要求——默认行为会往用户的
    PDF 里加标签；关日志与 MuPDF 报错是为了不污染 REPL 输出。
    """
    global _configured
    with _config_lock:
        if _configured:
            return
        import fitz

        import pdf2bib
        import pdf2doi

        for mod in (pdf2bib, pdf2doi):
            mod.config.set("verbose", False)
            mod.config.set("save_identifier_metadata", False)
        # 关掉 Google 搜索兜底：它靠抓取网页、又慢又不稳，且对学术 PDF 命中率低；
        # 关掉后仍会读元数据 / 文件名 / 正文找标识符，并做权威性校验。
        pdf2doi.config.set("websearch", False)
        # MuPDF 对损坏 PDF 的报错走 C 层直写 stderr，不走 logging，只能从源头关。
        fitz.TOOLS.mupdf_display_errors(False)
        _configured = True


def _run_with_timeout(fn, timeout: float):
    """在守护线程里跑 fn 并最多等 timeout 秒；超时抛 TimeoutError。

    用守护线程而不是线程池：卡住的线程若一直不返回，守护线程不会拖住解释器退出
    （REPL 场景下进程要能正常关掉）。超时后该线程仍在后台跑，但它做的事情是只读的
    HTTP 取数与返回，不会再影响调用方。

    只收 ``Exception``：``KeyboardInterrupt`` / ``SystemExit`` 这类不该被吞掉——
    它们在守护线程里终止该线程，调用方按「没拿到结果」处理（返回空书目）。

    Args:
        fn: 无参可调用，返回取数结果。
        timeout: 最长等待秒数。

    Returns:
        fn 的返回值（失败路径下为 None）。

    Raises:
        TimeoutError: 超过 timeout 仍未返回。
        Exception: fn 抛出的普通异常原样上抛。
    """
    box: dict = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except Exception as e:
            box["error"] = e

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join(timeout)
    if thread.is_alive():
        raise TimeoutError(f"书目提取超时（>{timeout:g}s）")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def _lookup(pdf_path: str) -> dict | None:
    """调用 pdf2bib 取一篇 PDF 的原始结果（含标识符与书目元数据）。

    Args:
        pdf_path: PDF 文件路径。

    Returns:
        dict | None: pdf2bib 的原始返回；无标识符时其 ``metadata`` 为空或为 None。
    """
    _ensure_configured()
    import pdf2bib

    return pdf2bib.pdf2bib(str(pdf_path))


def _authors_text(raw) -> str:
    """把 pdf2bib 的作者列表拼成 BibTeX 风格的 ``Family, Given`` 串。

    键生成取首个 ``[,\\s]`` 之前的分词当作者姓氏，各渲染器按 ``" and "`` 拆分作者，
    所以这里必须产出「姓, 名 and 姓, 名」的形态，不能改成逗号分隔的全名串。

    Args:
        raw: pdf2bib 的作者字段：``[{"given": …, "family": …}, …]``，也可能是字符串。

    Returns:
        str: 拼好的作者串；无作者时为空串。
    """
    if not raw:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    parts = []
    for item in raw:
        if isinstance(item, dict):
            family = str(item.get("family") or "").strip()
            given = str(item.get("given") or "").strip()
            if family and given:
                parts.append(f"{family}, {given}")
            elif family or given:
                parts.append(family or given)
        elif isinstance(item, str) and item.strip():
            parts.append(item.strip())
    return " and ".join(parts)


def _journal_text(meta: dict) -> str:
    """从书目字段里取期刊名（期刊论文取 journal，会议取 booktitle，预印本取 ejournal）。

    Args:
        meta: pdf2bib 的 ``metadata`` 字典。

    Returns:
        str: 期刊 / 会议 / 预印本源名；都取不到时为空串。
    """
    for key in ("journal", "booktitle", "ejournal"):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _to_bibmeta(result) -> "BibMeta":
    """把 pdf2bib 的返回映射成 BibMeta。

    Args:
        result: pdf2bib 的原始返回（dict 或 None）。

    Returns:
        BibMeta: 三项书目；任何一项缺失即留空串。
    """
    if not isinstance(result, dict):
        return BibMeta()
    meta = result.get("metadata")
    if not isinstance(meta, dict):
        return BibMeta()
    year = meta.get("year")
    return BibMeta(
        authors=_authors_text(meta.get("author")),
        year=str(year).strip() if year not in (None, "") else "",
        journal=_journal_text(meta),
    )


class PaperMeta(BaseModel):
    """首页兜底提取的结构化输出模型。

    Attributes:
        authors: 作者串（BibTeX 形式「姓, 名」，多位用 and 连接）；读不到为空串。
        year: 四位年份字符串；读不到为空串。
        journal: 期刊或会议名；读不到为空串。
    """

    authors: str = ""
    year: str = ""
    journal: str = ""


@dataclass(frozen=True)
class BibMeta:
    """取到的书目元数据。

    ``as_dict`` 的键与 `references.bib` 条目渲染用的键一致（作者/年份/期刊），
    空项不进字典——与「缺失的键不出现」的既有约定一致，`entry_text` 按 `.get()`
    取值，缺键即该字段留空。
    """

    authors: str = ""
    year: str = ""
    journal: str = ""

    def as_dict(self) -> dict:
        """转成 bib 条目用的字段字典（空项不进字典）。

        Returns:
            dict: 仅含非空项的 {键: 值}。
        """
        pairs = (("authors", self.authors), ("year", self.year),
                 ("journal", self.journal))
        return {k: v for k, v in pairs if v}


class PaperMetaExtractor:
    """PDF → 书目元数据（pdf2bib 取权威书目，取不到再用模型读首页兜底）。

    Attributes:
        _llm: LLMClient | None，兜底那一级的文本模型客户端；None 且未注入
            ``structured`` 时兜底不可用（只走 pdf2bib）
        _lookup: pdf2bib 取数函数（测试可注入桩，避免联网）
        _structured: StructuredOutput | None，结构化输出通道（惰性构造，测试可注入）
        _timeout: pdf2bib 单次取数超时（秒）
    """

    def __init__(self, llm=None, lookup=None, structured=None,
                 timeout: float = _CALL_TIMEOUT_SECONDS):
        """绑定两级取数依赖；两级的实现都可注入（测试传桩，避免真实联网/调模型）。

        Args:
            llm: LLMClient | None，首页兜底用的文本模型；None 表示不配兜底。
            lookup: 可调用 ``(pdf_path) -> dict | None``；None 时用 pdf2bib。
            structured: StructuredOutput | None，兜底的结构化输出通道（测试可注入）。
            timeout: pdf2bib 单次取数超时（秒）。
        """
        self._llm = llm
        self._lookup = lookup
        self._structured = structured
        self._timeout = timeout

    def _ensure_structured(self):
        """惰性构造兜底用的结构化输出通道（避免构造期就拉起 LLM 依赖）。

        Returns:
            StructuredOutput: 结构化输出通道。
        """
        if self._structured is None:
            from paperflow.core.llm import StructuredOutput
            self._structured = StructuredOutput(self._llm)
        return self._structured

    def from_pdf(self, pdf_path: str) -> BibMeta:
        """取一篇 PDF 的书目元数据；两级都失败就返回空书目且不抛。

        先走标识符取数，拿到任意一项即返回；取不到（无标识符 / 联网失败 / 超时 /
        文件读不动）才落到首页兜底。两级都自带降级，绝不因书目取不到而中断引用流程。

        Args:
            pdf_path: PDF 文件路径。

        Returns:
            BibMeta: 取到的书目元数据；拿不到时各项为空串。
        """
        meta = self._via_identifier(pdf_path)
        if meta.as_dict():
            return meta
        return self._via_first_page(pdf_path)

    def _via_identifier(self, pdf_path: str) -> BibMeta:
        """第一级：pdf2bib 取权威书目；失败返回空（由调用方决定是否兜底）。

        Args:
            pdf_path: PDF 文件路径。

        Returns:
            BibMeta: 取到的书目；失败或无标识符时各项为空。
        """
        fn = self._lookup or _lookup
        try:
            result = _run_with_timeout(lambda: fn(pdf_path), self._timeout)
        except Exception as e:
            logger.warning("标识符取数失败，转首页兜底（%s）：%s", pdf_path, e)
            return BibMeta()
        return _to_bibmeta(result)

    def _via_first_page(self, pdf_path: str) -> BibMeta:
        """第二级：模型读首页取书目；未配模型或失败返回空。

        Args:
            pdf_path: PDF 文件路径。

        Returns:
            BibMeta: 模型读到的书目；读不到时各项为空。
        """
        if self._llm is None and self._structured is None:
            # 没配模型 = 这一级不可用，不是错误（第一级失败就如实为空）
            return BibMeta()
        try:
            from paperflow.rag.parsers.pdf_extract import first_page_text
            text = first_page_text(pdf_path)[:FIRST_PAGE_CHARS]
        except Exception as e:
            logger.warning("读 PDF 首页失败，书目留空：%s", e)
            return BibMeta()
        if not text.strip():
            return BibMeta()
        try:
            out = asyncio.run(self._ensure_structured().extract(
                _PROMPT.format(text=text), PaperMeta))
        except Exception as e:
            logger.warning("首页书目兜底提取失败，书目留空：%s", e)
            return BibMeta()
        return BibMeta(
            authors=(out.authors or "").strip(),
            year=(out.year or "").strip(),
            journal=(out.journal or "").strip(),
        )
