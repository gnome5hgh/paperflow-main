"""PDF 图表提取管线编排：pdffigures2 8 步（文本 → 布局 → 图注 → 图形 → 分类 → 图检测 → 渲染）。

替代旧启发式 FigureExtractor。编排顺序与 pdffigures2 的 Main 对齐：
1. extract_text        rawdict → word/line/paragraph
2. strip_formatting    去页眉/页脚/页码
3. build_document_layout   双栏/字号/行宽/中位行距等文档级统计
4. find_captions       全文档图注起始识别 + filter sieve 消歧
5. 逐页 extract_graphics   矢量 + 光栅图形区
6. 逐页 build_captions     图注起始行向后扩展成完整图注段落
7. 逐页 classify_regions   正文/图内文本分类
8. 逐页 located_figures    为每图注构建候选区域、打分、取最优配置
9. 组装每个区域 → schemas.Figure（渲染可选：调用方只要区域与文本时不渲染）

消费方分两类：看图（FigureAnalyzer / analyze_figures 工具）用 number/caption/
image_bytes/mime；造检索块（索引侧）用 caption/image_text/region_boundary/page，
并以 render_images=False 跳过渲染。图与表都产出——表格单元格里的数值常是检索目标。
"""
from __future__ import annotations

import fitz
fitz.TOOLS.mupdf_display_errors(False)  # C 层 stderr 告警（损坏对象/字体）不糊屏：失败仍经工具返回值可见

from paperflow.vision.parsers.caption import (
    build_captions,
    find_captions,
    strip_caption_lines,
)
from paperflow.vision.parsers.document_layout import build_document_layout
from paperflow.vision.detectors.figure_detector import located_figures
from paperflow.vision.services.renderer import render_figure
from paperflow.vision.schemas import Figure
from paperflow.vision.parsers.text_extractor import Page, extract_text, strip_formatting
from paperflow.vision.parsers.graphics import extract_graphics
from paperflow.vision.detectors.region_classifier import classify_regions


def _parse_number(name: str) -> int:
    """图号从 name 解析出整数；解析失败归 0（两段式图号 "3.1" 等当前不支持）。

    Args:
        name: 图号原始字符串（如 "1" 或 "3.1"）。

    Returns:
        解析出的整数，若无法解析则返回 0。
    """
    try:
        return int(name)
    except (TypeError, ValueError):
        return 0


class FigureExtractor:
    """用 pdffigures2 8 步管线从 PDF 提取图表，产出 schemas.Figure 列表。

    此类负责整个提取流程的编排，将文本抽取、布局统计、图注识别、图形提取、
    文本分类、图区域检测、渲染等步骤串联起来，最终返回视觉分析可用的 Figure 对象列表。
    """

    def extract(self, path: str, render_images: bool = True) -> list[Figure]:
        """提取 PDF 中所有图表对象（图与表都产出）。

        Args:
            path: PDF 文件绝对路径。
            render_images: 是否把区域栅格化成 PNG。只要区域定位与区域文本的调用方
                （如索引侧造媒体块，不落图）传 False，省掉整篇的渲染开销——区域、
                注文与图内文本照常给出。

        Returns:
            list[Figure]：图表对象列表（含 fig_type 为 Table 的表）；无图或布局信息
            不足（扫描件/纯图文档）返回空列表。render_images=False 时 image_bytes
            为空字节、mime 为空串。

        注意：
            本方法会打开 PDF 文档并在 finally 中关闭，确保资源释放。
            提取过程依赖文本布局统计，若 layout 为 None（即文本信息严重不足），
            则直接返回空列表，不做后续检测。
        """
        # 一次打开、两处复用：extract_text 接收 fitz.Document 不接管其生命周期，
        # 后续逐页取图形区 / 渲染图区域仍靠这份 doc。两套「页」并存——
        # fitz 页（doc 按索引取）供渲染与光栅图，我们自己的 Page（pages 列表）
        # 供图注/图形/分类/图检测；两套按页码一一对应。
        doc = fitz.open(path)
        try:
            # 步骤1-2：抽取文本并清除页眉/页脚/页码
            pages = strip_formatting(extract_text(doc))
            # 步骤3：构建文档级布局统计（双栏、标准字号、行宽等）
            layout = build_document_layout(pages)
            if layout is None:
                # 文本几乎抽不出来，给不出可信布局统计 → 无从检测图
                return []
            # 步骤4：全文档图注起始识别（含消歧）
            starts = find_captions(pages, layout)
            # 步骤5-9：逐页处理
            return self._process_pages(doc, pages, starts, layout, render_images)
        finally:
            doc.close()

    def _process_pages(
        self, doc, pages: list[Page], starts, layout, render_images: bool = True
    ) -> list[Figure]:
        """逐页执行 图形 → 图注扩展 → 分类 → 图检测 →（可选渲染），汇总成 Figure 列表。

        图注起始（starts）是全文级别识别的，按页码过滤出本页的再传给 build_captions
        ——build_captions 靠行对象身份对齐，必须复用 find_captions 传入的同一批 Page。

        Args:
            doc: 已打开的 fitz.Document 对象（保持打开状态）。
            pages: 经过 strip_formatting 后的 Page 列表（与 doc 页码一一对应）。
            starts: 全文档的图注起始候选列表（CaptionStart）。
            layout: 文档级布局统计。

        Returns:
            Figure 对象列表（仅包含类型为 Figure 的图，Table 被过滤掉）。
        """
        figures: list[Figure] = []
        for index, text_page in enumerate(pages):
            # 过滤出属于当前页的图注起始行
            page_starts = [s for s in starts if s.page == text_page.page_number]
            if not page_starts:
                continue  # 本页无图注即无图，跳过渲染
            # 获取对应页的 PyMuPDF 对象（用于图形提取和渲染）
            fitz_page = doc[index]
            # 步骤5：提取当前页图形（矢量 + 光栅），并分离侧栏
            graphics, non_figure_graphics = extract_graphics(fitz_page)
            # 步骤6：将图注起始行扩展为完整图注段落（CaptionParagraph）
            captions = build_captions(
                page_starts, graphics, text_page, layout.median_line_spacing
            )
            # 图注行从正文段落剥离：否则小字图注会被分类误判成图内文本、甚至
            # 抑制图边框检测（removeSpans 语义，见 caption.strip_caption_lines）
            strip_caption_lines(text_page, captions)
            # 步骤7：将文本分类为正文 / 图内文本，并检测图形包围盒
            classified = classify_regions(
                text_page, captions, graphics, non_figure_graphics, layout
            )
            # 步骤8：为每个图注定位图区域，取最优配置
            located = located_figures(classified, layout)
            # 步骤9：逐个检测到的区域组装 Figure 对象（图与表都产出——表格单元格里
            # 的数值常是检索目标，只在章节正文里找不到它们）
            for f in located.figures:
                if render_images:
                    # 将 region_boundary 裁剪区域渲染为 PNG 字节
                    image_bytes, mime = render_figure(fitz_page, f["region_boundary"])
                else:
                    image_bytes, mime = b"", ""
                figures.append(Figure(
                    number=_parse_number(f["name"]),
                    caption=f["caption_text"],
                    page=f["page"],
                    image_bytes=image_bytes,
                    mime=mime,
                    name=f["name"],
                    fig_type=f["fig_type"],
                    image_text=" ".join(f["image_text"]),
                    caption_boundary=f["caption_boundary"],
                    region_boundary=f["region_boundary"],
                ))
        return figures