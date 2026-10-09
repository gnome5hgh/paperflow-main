"""图区渲染：把 FigureDetector 定位出的图区域从 PDF 页栅格化成 PNG。

pdffigures2 的 Rasterizer 对应物——FigureDetector 产出的是矢量/文本层几何，
视觉模型看图需要像素，这里用 PyMuPDF 按图区域裁剪渲染。输入必须是仍然打开的
fitz 文档里的 Page（渲染由所属文档驱动），区域用我们自己的 Box 几何传入。

已知偏差（正式接受）：pdffigures2 的 FigureRenderer 在渲染前还会 expandFigureBounds
（MaxExpand=20）把图边界向四周扩张，以免裁掉紧贴图区的文字；本实现做 bare clip
（直接按检测出的 region 裁剪，不扩张）。已知质量缺口：图边界紧邻图外
文字时渲染可能裁掉少量文字，见 render_figure docstring。
"""
from __future__ import annotations

import fitz
fitz.TOOLS.mupdf_display_errors(False)  # C 层 stderr 告警（损坏对象/字体）不糊屏：失败仍经工具返回值可见

from paperflow.vision.common.geometry import Box


def render_figure(page, region: Box, dpi: int = 150) -> tuple[bytes, str]:
    """将检测出的图区域从 PDF 页面渲染为 PNG 图像字节。

    本函数使用 PyMuPDF 的 get_pixmap 按给定区域裁剪并渲染，
    产出的 PNG 字节可直接用于 base64 编码或落盘。

    Args:
        page: PyMuPDF Page 对象，所属文档必须保持打开状态（在调用期间不能关闭）。
        region: 图区域，Box 类型，坐标单位为 pt，原点在页面左上角，y 轴向下。
        dpi: 渲染分辨率（dots per inch），默认 150。
            该值在视觉模型清晰度与传输/处理成本之间取得平衡：
            - 太低（如 72）会导致文字模糊，影响模型识别。
            - 太高（如 300）会显著增加图片字节数，增加推理延迟和 token 消耗。

    Returns:
        tuple[bytes, str]:
            - PNG 格式的图像字节（可直接写入文件或传输）。
            - MIME 类型字符串，固定为 "image/png"。

    边界条件与注意事项：
        1. 若 region 部分或全部超出页面边界，`clip &= page.rect` 会将其裁剪到页面可视区域，
           get_pixmap 同样会自动裁剪，但显式求交可避免传递无效矩形。
        2. 本实现不向图区域四周扩展边界（与 pdffigures2 的 FigureRenderer 不同）。
           若图注或图形边缘紧贴 region 边界，渲染结果可能裁掉少量紧邻的文字/图形元素。
           这是为了严格遵循下游视觉模型的输入预期（只包含检测区域），且避免引入无关内容。
        3. 图像输出始终为 PNG 格式，因为 PNG 无损且广泛支持。
    """
    # 将 Box 转换为 PyMuPDF 的 Rect 对象
    clip = fitz.Rect(region.x1, region.y1, region.x2, region.y2)
    # 若区域超出页面边界，裁剪到页面内（防御性操作，get_pixmap 也会自动处理）
    clip &= page.rect
    # 按指定 DPI 渲染该区域为像素图
    pix = page.get_pixmap(clip=clip, dpi=dpi)
    # 输出为 PNG 格式字节流
    return pix.tobytes("png"), "image/png"