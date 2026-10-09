"""首页书目元数据提取：读 PDF 首页文本，用结构化输出取作者 / 年份 / 期刊。

**只做这一件事**——标题不从这里取（标题是解析器的一层产出，见
`rag/parsers/pdf_extract.pdf_title`）。书目元数据只有引用域消费（语料索引是唯一调用点），
所以代码归引用域。

**为什么用模型而不是本地启发式**：模型能把依据回显（读到的作者行、版权行），拿不到
就如实上报；本地启发式「拿到了错的」不会触发兜底，会静默写出年份错误的 bib 条目——
比缺字段更难发现。代价是这里有一次 LLM 调用，所以调用方按 mtime 缓存结果，别重复调用。
"""
import asyncio
import logging
from dataclasses import dataclass

from pydantic import BaseModel

logger = logging.getLogger(__name__)

#: 送进模型的首页文本上限（字符）：作者、年份、期刊都印在首页最上面一段，
#: 取多了只会把摘要正文也塞进去，白烧 token。
FIRST_PAGE_CHARS = 2000

_PROMPT = (
    "从下面的论文首页文本里读出版权信息中的书目元数据。只读文本里真的写了的内容，"
    "任何一项读不到就留空字符串——**不要推测、不要用常识补全**，宁可空着。\n"
    "输出 JSON：{{authors, year, journal}}。\n"
    "authors 给作者全名串（多人用逗号分隔）；year 给四位年份；journal 给期刊或会议名。\n"
    "首页文本：\n{text}"
)


class PaperMeta(BaseModel):
    """书目元数据的结构化输出模型。

    Attributes:
        authors: 作者串（多人用逗号分隔）；读不到为空串。
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
    """首页文本 → 书目元数据（一次结构化 LLM 调用，失败即返回空）。

    Attributes:
        _llm: LLMClient，文本模型客户端（引用域从 config.llm 惰性构造）
        _structured: StructuredOutput | None，结构化输出通道（惰性构造，测试可注入）
    """

    def __init__(self, llm, structured=None):
        """绑定模型客户端；结构化输出通道可注入（测试传桩）。

        Args:
            llm: LLMClient 实例（书目提取用的文本模型）。
            structured: StructuredOutput 实例；None 时按 llm 惰性构造。
        """
        self._llm = llm
        self._structured = structured

    def _ensure_structured(self):
        """惰性构造结构化输出通道（避免构造期就拉起 LLM 依赖）。

        Returns:
            StructuredOutput: 结构化输出通道。
        """
        if self._structured is None:
            from paperflow.core.llm import StructuredOutput
            self._structured = StructuredOutput(self._llm)
        return self._structured

    def from_pdf(self, pdf_path: str) -> BibMeta:
        """读 PDF 首页并取书目元数据；任何失败都返回空书目且不抛。

        降级语义：模型未配置、调用失败、首页读不出来、输出不合法——一律返回空
        `BibMeta`。调用方据此退化为「缺书目的引用」并如实上报，绝不因书目取不到
        而中断引用流程。

        Args:
            pdf_path: PDF 文件路径。

        Returns:
            BibMeta: 取到的书目元数据；拿不到时各项为空串。
        """
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
            logger.warning("首页书目提取失败，书目留空：%s", e)
            return BibMeta()
        return BibMeta(
            authors=(out.authors or "").strip(),
            year=(out.year or "").strip(),
            journal=(out.journal or "").strip(),
        )
