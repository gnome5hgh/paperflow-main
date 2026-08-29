"""PDF 图表提取：图注定位 + 栅格图/区域渲染取图。与 GROBID 正交——GROBID 只给
文本与图注，图片本身恒由本模块负责。"""
import re

#: 图注识别：行首出现 Fig./Figure/图 + 数字（如 "Fig. 3:" / "Figure 4 " / "图 2."）
_CAPTION_RE = re.compile(r"^(?:Fig(?:ure)?\.?\s*|图\s*)(\d+)", re.IGNORECASE)
#: 图注与上方栅格图的垂直间距超过页面高度 20% 即视为不相关（行内引用间距很小）
_MAX_GAP_RATIO = 0.2
#: 区域渲染兜底的最小图区高度（pt），低于此值视为无图（正文行内引用）
_MIN_RENDER_HEIGHT = 20.0


class FigureExtractor:
    """用 PyMuPDF 从 PDF 提取图表：识别图注块，为每个图注定位图区并取图。

    图区定位启发式（v1，够用不求全）：
    1. 栅格优先：图注上方、水平有交叠的图片（get_image_info）取面积最大者，
       经 extract_image 拿原始字节（保真度最高）。
    2. 渲染兜底：上方无栅格图（matplotlib 矢量图等）时，把「上一文本块底边 →
       图注顶边」区域渲染成 PNG；区域过小（<20pt，正文行内引用 "Fig. N shows"）
       或区域内无矢量绘制 → 跳过，不硬凑。
    3. 去重：同一栅格 xref / 同一渲染区域只出一个 Figure（子图 a/b 场景留 v2）。
    """

    def extract(self, path: str) -> list:
        """提取 PDF 中所有图表对象。

        Args:
            path: PDF 文件绝对路径。

        Returns:
            list[Figure]：图表对象列表，无图返回空列表。
        """
        import fitz

        from paperflow.vision.schemas import Figure

        figures: list = []
        seen: set = set()
        with fitz.open(path) as doc:
            for pno, page in enumerate(doc, start=1):
                raster = page.get_image_info(xrefs=True)
                text_blocks = [b for b in page.get_text("dict").get("blocks", [])
                               if b.get("type") == 0]
                text_blocks.sort(key=lambda b: b["bbox"][1])  # 自上而下
                for i, blk in enumerate(text_blocks):
                    caption = _block_text(blk)
                    m = _CAPTION_RE.match(caption)
                    if not m:
                        continue
                    number = int(m.group(1))
                    bbox = blk["bbox"]
                    hit = self._raster_above(page, raster, bbox)
                    if hit is not None:
                        key = (pno, hit["xref"])
                        if key in seen:
                            continue
                        seen.add(key)
                        data = doc.extract_image(hit["xref"])
                        figures.append(Figure(
                            number=number, caption=caption.strip(), page=pno,
                            image_bytes=data["image"], mime=f"image/{data['ext']}"))
                        continue
                    region = self._render_region(page, text_blocks, i, bbox)
                    if region is None:
                        continue
                    key = (pno, round(region.y0), round(region.y1))
                    if key in seen:
                        continue
                    seen.add(key)
                    pix = page.get_pixmap(clip=region)
                    figures.append(Figure(
                        number=number, caption=caption.strip(), page=pno,
                        image_bytes=pix.tobytes("png"), mime="image/png"))
        return figures

    def _raster_above(self, page, raster, caption_bbox):
        """图注上方、水平有交叠、间距在阈值内的最大栅格图；无则 None。

        返回 get_image_info 的元素（含 xref/bbox），供调用方 extract_image。
        """
        best = None
        for item in raster:
            xref = item.get("xref", 0)
            if xref <= 0:
                continue
            ib = item["bbox"]
            if ib[3] <= caption_bbox[1] and (caption_bbox[1] - ib[3]) <= page.rect.height * _MAX_GAP_RATIO:
                if ib[0] < caption_bbox[2] and ib[2] > caption_bbox[0]:  # 水平交叠防跨列误配
                    area = (ib[2] - ib[0]) * (ib[3] - ib[1])
                    if best is None or area > best[0]:
                        best = (area, item)
        return best[1] if best else None

    def _render_region(self, page, text_blocks, i, caption_bbox):
        """矢量渲染兜底的图区矩形：上一文本块底边 → 图注顶边。

        区域高度须 ≥ _MIN_RENDER_HEIGHT 且区域内含矢量绘制（get_drawings 命中），
        否则视为正文行内引用返回 None。返回 fitz.Rect 或 None。
        """
        import fitz

        prev_bottom = text_blocks[i - 1]["bbox"][3] if i > 0 else 0.0
        region = fitz.Rect(caption_bbox[0] - 2, prev_bottom,
                           caption_bbox[2] + 2, caption_bbox[1])
        region &= page.rect
        if region.height < _MIN_RENDER_HEIGHT:
            return None
        for d in page.get_drawings():
            if fitz.Rect(d["rect"]).intersects(region):
                return region
        return None


def _block_text(block) -> str:
    """拼接一个文本块的纯文本（多行 span 顺序拼）。"""
    return "".join(span.get("text", "")
                   for line in block.get("lines", [])
                   for span in line.get("spans", []))
