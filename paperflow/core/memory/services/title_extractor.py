"""权威标题提取：五级降级链（搜索元数据 > GROBID > LLM > pdftitle > PyMuPDF）。

任何一级都不取 pdf 文件名——文件名是存储产物（下载时可能是乱码/编号），
标题必须是论文原文的权威元数据，否则会污染 unread_list/history_list，
后续按标题去重/移除全部失准。
"""
from dataclasses import dataclass


@dataclass
class TitleResult:
    """标题提取结果：title + 命中的来源（search/grobid/llm/pdftitle/pymupdf）。

    全失败时 title 为 None，由调用方提示用户提供标题。
    """

    title: str | None = None
    source: str = ""


class TitleExtractor:
    """按序尝试各层，首个命中的 title 返回。

    grobid/llm 为可注入依赖（测试用 stub）；pdftitle/pymupdf 用 import-guard
    按 use_* 开关启用（pdftitle 是可选依赖，未装则跳过）。
    """

    def __init__(self, grobid=None, llm=None, use_pdftitle=True, use_pymupdf=True):
        """注入各层依赖；use_* 控制可选层是否启用。"""
        self.grobid = grobid
        self.llm = llm
        self.use_pdftitle = use_pdftitle
        self.use_pymupdf = use_pymupdf

    def extract(self, pdf_path: str | None = None,
                search_meta: dict | None = None) -> TitleResult:
        """按五级降级链提取标题，首个命中即返回；全失败返回空 TitleResult。"""
        # ① 搜索元数据（最权威、免费）
        if search_meta and search_meta.get("title"):
            return TitleResult(title=search_meta["title"],
                               source=search_meta.get("source", "search"))
        if not pdf_path:
            return TitleResult()

        # ② GROBID（本地 REST）
        if self.grobid is not None:
            t = self.grobid.extract_title(pdf_path)
            if t:
                return TitleResult(title=t, source="grobid")

        # ③ LLM 提取（首页文本）
        if self.llm is not None:
            t = self._llm_extract(pdf_path)
            if t:
                return TitleResult(title=t, source="llm")

        # ④ pdftitle
        if self.use_pdftitle:
            t = self._pdftitle_extract(pdf_path)
            if t:
                return TitleResult(title=t, source="pdftitle")

        # ⑤ PyMuPDF 首页启发式
        if self.use_pymupdf:
            t = self._pymupdf_extract(pdf_path)
            if t:
                return TitleResult(title=t, source="pymupdf")

        return TitleResult()     # 全失败：调用方提示用户提供标题

    def _llm_extract(self, pdf_path: str) -> str | None:
        """读 PDF 首页文本（元数据 + 前 ~1000 字），LLM 判断标题。"""
        first_page = self._read_first_page(pdf_path)
        if not first_page:
            return None
        prompt = ("你是论文标题识别器。从以下论文首页文本（元数据+正文前段）判断论文标题。"
                  "输出 JSON：{title}。\n首页文本：\n" + first_page[:1200])
        import asyncio
        from pydantic import BaseModel
        class _Title(BaseModel):
            title: str
        r = asyncio.run(self.llm.extract(prompt=prompt, schema=_Title))
        return getattr(r, "title", None) or None

    def _read_first_page(self, pdf_path: str) -> str:
        """读 PDF 首页前 1000 字 + 元数据标题；读取失败返回空字符串（降级到下一层）。"""
        try:
            import fitz
            doc = fitz.open(pdf_path)
            text = doc[0].get_text("text")[:1000]
            meta = doc.metadata or {}
            doc.close()
            return f"{meta.get('title','')}\n{text}".strip()
        except Exception:
            return ""

    def _pdftitle_extract(self, pdf_path: str) -> str | None:
        """用 pdftitle 库提取标题；依赖未装/提取失败返回 None（跳过这一层）。"""
        try:
            import pdftitle
            return pdftitle.get_title_from_pdf(pdf_path) or None
        except Exception:
            return None

    def _pymupdf_extract(self, pdf_path: str) -> str | None:
        """首页最大字号近顶部文本当标题（启发式兜底）。"""
        import fitz
        try:
            doc = fitz.open(pdf_path)
            page = doc[0]
            spans = []
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        spans.append((span["text"], span["size"], span["bbox"][1]))
            doc.close()
            if not spans:
                return None
            max_size = max(s[1] for s in spans)
            # 最大字号里取最靠顶部的（bbox y 最小）——标题通常既大又近页首
            top = min((s for s in spans if s[1] == max_size), key=lambda s: s[2])
            return top[0].strip() or None
        except Exception:
            return None
