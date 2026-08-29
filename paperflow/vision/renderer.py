"""图区渲染：把 FigureDetector 定位出的图区域从 PDF 页栅格化成 PNG。

pdffigures2 的 Rasterizer 对应物——FigureDetector 产出的是矢量/文本层几何，
视觉模型看图需要像素，这里用 PyMuPDF 按图区域裁剪渲染。输入必须是仍然打开的
fitz 文档里的 Page（渲染由所属文档驱动），区域用我们自己的 Box 几何传入。
"""
from __future__ import annotations

import fitz

from paperflow.vision.geometry import Box


def render_figure(page, region: Box, dpi: int = 150) -> tuple[bytes, str]:
    """把一个图区域渲染成 PNG 字节。

    Args:
        page: PyMuPDF Page 对象（所属文档须保持打开）。
        region: 图区域（Box，pt 坐标，原点左上、y 向下）。
        dpi: 渲染分辨率。默认 150，在视觉模型看图清晰度与 token/传输成本间取平衡。

    Returns:
        (PNG bytes, "image/png")：直接可用于 base64 data URL / 落盘。
    """
    clip = fitz.Rect(region.x1, region.y1, region.x2, region.y2)
    # 区域可能贴页面边缘，get_pixmap 会自行裁到页内；与页面矩形求交只为显式兜底
    clip &= page.rect
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    return pix.tobytes("png"), "image/png"
