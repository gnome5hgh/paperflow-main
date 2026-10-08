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

    Attributes:
        title: str | None，提取到的标题；全链失败为 None
        source: str，命中来源（search/grobid/llm/pdftitle/pymupdf）；未命中为空串
    """

    title: str | None = None
    source: str = ""


class TitleExtractor:
    """按序尝试各层，首个命中的 title 返回。

    grobid/llm 为可注入依赖（测试用 stub）；pdftitle/pymupdf 用 import-guard
    按 use_* 开关启用（pdftitle 是可选依赖，未装则跳过）。

    Attributes:
        grobid: GROBID 客户端 | None，需实现 extract_title(pdf_path) -> str
        llm: LLM 客户端 | None，需实现 extract(prompt, schema) -> Model
        use_pdftitle: bool，是否启用 pdftitle 库层（可选依赖，未装则跳过）
        use_pymupdf: bool，是否启用 PyMuPDF 启发式兜底层
    """

    def __init__(self, grobid=None, llm=None, use_pdftitle=True, use_pymupdf=True):
        """注入各层依赖；use_* 控制可选层是否启用。

        Args:
            grobid: GROBID 客户端实例（需实现 extract_title(pdf_path) -> str）。
            llm: LLM 客户端实例（需实现 extract(prompt, schema) -> Model）。
            use_pdftitle: 是否启用 pdftitle 库层（该库是可选依赖）。
            use_pymupdf: 是否启用 PyMuPDF 启发式兜底层（通常开启）。
        """
        self.grobid = grobid
        self.llm = llm
        self.use_pdftitle = use_pdftitle
        self.use_pymupdf = use_pymupdf

    def extract(self, pdf_path: str | None = None,
                search_meta: dict | None = None) -> TitleResult:
        """按五级降级链提取标题，首个命中即返回；全失败返回空 TitleResult。

        Args:
            pdf_path: PDF 文件路径（若 search_meta 已命中，可不传）。
            search_meta: 搜索元数据字典（如论文数据库返回的元信息），
                必须包含 "title" 键且非空才命中。

        Returns:
            TitleResult 对象（含标题与来源）。

        边界条件与策略：
            - 搜索元数据优先级最高，因为外部数据库（如 arXiv、Crossref）的标题
              最准确，且免费无需本地计算。
            - 若 search_meta 命中，直接返回，不依赖 PDF 文件是否存在。
            - 若 search_meta 未命中但 pdf_path 为空，直接返回空（无法继续提取）。
            - GROBID、LLM、pdftitle、PyMuPDF 依次降级，任一层返回非空即停。
        """
        # ① 搜索元数据（最权威、免费，且不依赖 PDF 文件）
        if search_meta and search_meta.get("title"):
            return TitleResult(title=search_meta["title"],
                               source=search_meta.get("source", "search"))

        # 若无 PDF 路径，后续层级无法工作，直接返回空
        if not pdf_path:
            return TitleResult()

        # ② GROBID（本地 REST 服务，解析 PDF 的 XML 结构，准确率高）
        if self.grobid is not None:
            t = self.grobid.extract_title(pdf_path)
            if t:
                return TitleResult(title=t, source="grobid")

        # ③ LLM 提取（读取首页文本，由模型推断标题，适合格式不规范或非英语论文）
        if self.llm is not None:
            t = self._llm_extract(pdf_path)
            if t:
                return TitleResult(title=t, source="llm")

        # ④ pdftitle（基于标题页布局启发式的轻量库，可选依赖）
        if self.use_pdftitle:
            t = self._pdftitle_extract(pdf_path)
            if t:
                return TitleResult(title=t, source="pdftitle")

        # ⑤ PyMuPDF 首页启发式（最大字号 + 最靠顶部，纯文本布局兜底）
        if self.use_pymupdf:
            t = self._pymupdf_extract(pdf_path)
            if t:
                return TitleResult(title=t, source="pymupdf")

        # 全失败：调用方应提示用户手动提供标题
        return TitleResult()

    def _llm_extract(self, pdf_path: str) -> str | None:
        """读 PDF 首页文本（元数据 + 前 ~1000 字），LLM 判断标题。

        Args:
            pdf_path: PDF 文件路径。

        Returns:
            提取出的标题字符串，或 None。

        注意：
            - 使用 asyncio.run() 调用异步 LLM 客户端，若已在事件循环中调用会报错，
              因此本方法应在同步上下文或主线程中使用。
            - 首页文本截取 1200 字符（含元数据标题），平衡上下文长度与信息完整性。
            - 使用 Pydantic 模型约束 LLM 输出，确保返回结构包含 "title" 字段。
        """
        first_page = self._read_first_page(pdf_path)
        if not first_page:
            return None
        prompt = ("你是论文标题识别器。从以下论文首页文本（元数据+正文前段）判断论文标题。"
                  "输出 JSON：{title}。\n首页文本：\n" + first_page[:1200])
        import asyncio
        from pydantic import BaseModel

        class _Title(BaseModel):
            """LLM 标题提取的结构化输出模型（单字段）。

            Attributes:
                title: str，LLM 判定出的论文标题
            """
            title: str

        # 运行异步 LLM 提取（同步封装）
        r = asyncio.run(self.llm.extract(prompt=prompt, schema=_Title))
        return getattr(r, "title", None) or None

    def _read_first_page(self, pdf_path: str) -> str:
        """读 PDF 首页前 1000 字 + 元数据标题；读取失败返回空字符串（降级到下一层）。

        Args:
            pdf_path: PDF 文件路径。

        Returns:
            拼接了元数据标题和首页文本的字符串（截断至合适长度）。

        设计考虑：
            - PyMuPDF 是必备依赖（项目核心依赖），此处无需 try-import，直接 import。
            - 任何异常（文件损坏、权限、格式问题）都捕获并返回空字符串，保证降级链
              不会因单个 PDF 异常而中断。
        """
        try:
            import fitz
            fitz.TOOLS.mupdf_display_errors(False)  # C 层 stderr 告警不糊屏
            doc = fitz.open(pdf_path)
            text = doc[0].get_text("text")[:1000]  # 只取首页前 1000 字符
            meta = doc.metadata or {}
            doc.close()
            # 合并元数据标题与正文文本，便于 LLM 综合判断
            return f"{meta.get('title','')}\n{text}".strip()
        except Exception:
            # 任何读取失败（IOError、fitz 解析错误等）均返回空，触发下一层
            return ""

    def _pdftitle_extract(self, pdf_path: str) -> str | None:
        """用 pdftitle 库提取标题；依赖未装/提取失败返回 None（跳过这一层）。

        Args:
            pdf_path: PDF 文件路径。

        Returns:
            标题字符串或 None。

        注意：pdftitle 是可选依赖，使用 try-import 捕获 ImportError，
        避免因依赖缺失导致整个提取器崩溃。
        """
        try:
            import pdftitle
            return pdftitle.get_title_from_pdf(pdf_path) or None
        except Exception:
            # 库未安装、解析失败均返回 None
            return None

    def _pymupdf_extract(self, pdf_path: str) -> str | None:
        """首页最大字号近顶部文本当标题（启发式兜底）。

        Args:
            pdf_path: PDF 文件路径。

        Returns:
            标题字符串或 None。

        算法思路：
            1. 使用 fitz 解析首页所有文本块（dict 格式），提取 (文本, 字号, y坐标)。
            2. 找到最大字号（max_size）。
            3. 在最大字号的块中，选择 y 坐标最小（即最靠页面顶部）的文本作为标题。

        合理性：
            绝大多数学术论文/文档的标题字号明显大于正文，且位于页面顶部区域。
            这一启发式在格式标准的 PDF 中准确率很高，作为最后的兜底方案足够可靠。
        """
        import fitz
        fitz.TOOLS.mupdf_display_errors(False)  # C 层 stderr 告警不糊屏
        try:
            doc = fitz.open(pdf_path)
            page = doc[0]
            spans = []
            # 从页面字典中提取所有 span（文本片段）
            for block in page.get_text("dict").get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        # 存储：文本内容、字号大小、顶部边界 y 坐标
                        spans.append((span["text"], span["size"], span["bbox"][1]))
            doc.close()
            if not spans:
                return None

            # 找出最大字号
            max_size = max(s[1] for s in spans)
            # 在最大字号中，选取最靠近顶部（y 最小）的文本
            top = min((s for s in spans if s[1] == max_size), key=lambda s: s[2])
            return top[0].strip() or None
        except Exception:
            # 任何解析异常均返回 None，不阻断上层调用
            return None