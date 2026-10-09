"""图形元素提取：从 PDF 页提取图形包围盒（矢量 path + 光栅图）。

pdffigures2 的 GraphicsExtractor 移植（GraphicBBDetector.scala + GraphicsExtractor.scala）：
- 矢量 = `page.get_drawings()` 每 item 的 `rect`（fill/stroke 颜色判白过滤，照 isWhite）；
- 光栅 = `page.get_image_info(xrefs=True)` 每 item 的 `bbox`；
- 合并 = 侧栏分离 + 相交聚类（mergeBoxes，容差 2pt），产出 (graphics, nonFigureGraphics)。

坐标约定：PyMuPDF 原样（原点左上、y 向下，pt），直接映射 Box(x1,y1,x2,y2)。
本层接口只接收 page、不含文本/段落——GraphicsExtractor.scala 里依赖段落的
「页眉规则线检测（HeaderLineMinWidthPercent）」与「混入正文的小图形过滤
（MixedInGraphicMaxSize=70）」在此**正式接受未实现**（vs pdffigures2 的已知偏差，
接受声明见 extract_graphics docstring）。OCRed/整页扫描图需 FindGraphicsRaster 的
全页光栅连通域检测，也不在本层范围。
"""
from __future__ import annotations

from paperflow.vision.common.geometry import Box, Box_container

#: 相交聚类容差（GraphicClusteringTolerance=2，pt）：两框相距 ≤2pt 视为相交合并
_GRAPHIC_CLUSTERING_TOLERANCE = 2.0


def extract_graphics(page, ignore_white: bool = True) -> tuple[list[Box], list[Box]]:
    """提取页面上的图形元素包围盒，返回 (graphics, nonFigureGraphics)。

    已知偏差（正式接受）：页眉规则线检测与混入正文的小图形过滤未实现——两者都
    依赖段落文本/布局（本层只接收 page），且无真实 PDF 可验证移植新启发式的行为。
    影响：页眉线/正文小图形会留在 graphics，可能给邻近 proposal 误加
    ContainsGraphicBonus 或触发 _box_cuts_figure 误删；实际影响 modest——页眉线
    通常远离图区，正文小图形面积小、对图检测打分影响有限。

    Args:
        page: PyMuPDF Page 对象。
        ignore_white: 是否过滤白 fill/stroke 的矢量图形（白底占位、水印等，照 isWhite）。
            True 滤掉；False 保留全部。

    Returns:
        (graphics, nonFigureGraphics)：graphics 是相交聚类后的图形区；
        nonFigureGraphics 是贴着整页一侧、高度占满页面的「侧栏/色带」，非图、进正文分类。

    算法：
        1. 从 PyMuPDF 提取所有矢量路径（get_drawings）和光栅图像（get_image_info）。
        2. 对矢量图形做白图过滤（若启用）。
        3. 将原始图形列表按“是否侧栏”分离：高度占满页面的左右侧边装饰归入 nonFigureGraphics。
        4. 对剩余的图形做相交聚类（_merge_boxes），将相距 ≤2pt 的框合并，得到最终 graphics。
    """
    # 矢量 + 光栅合并成原始图形序列（对应 extractRawGraphics：GraphicBBDetector 的
    # path 包围盒 + drawImage 的图像包围盒）
    raw = _vector_graphics(page, ignore_white) + _raster_graphics(page)
    bounds = _page_bounds(page)

    # 侧栏分离（GraphicsExtractor 的 sidePanel 划分）：整页高度的侧边色带/索引
    # 通常不是图，单独归入 nonFigureGraphics
    side_panel = []
    graphics = []
    for b in raw:
        if _is_side_panel(b, bounds):
            side_panel.append(b)
        else:
            graphics.append(b)

    # 相交聚类：不断合并相交（含容差）的框直到两两不相交，产出最终图形区
    return _merge_boxes(graphics, _GRAPHIC_CLUSTERING_TOLERANCE), side_panel


def _page_bounds(page) -> Box:
    """页面可视范围（PyMuPDF 已做旋转归一，等价 PDFBox 的 crop box）。

    Args:
        page: PyMuPDF Page 对象。

    Returns:
        页面边界的 Box 对象。
    """
    r = page.rect
    return Box(r.x0, r.y0, r.x1, r.y1)


def _vector_graphics(page, ignore_white: bool) -> list[Box]:
    """矢量图形：get_drawings 每个 path 的 rect，按 fill/stroke 颜色做白图过滤。

    对应 GraphicBBDetector：fill/stroke 路径包围盒 + isWhite 白图过滤。
    type 含 'f'/'s'/'fs' 及 even-odd 变体（'f*' 等）；纯裁剪项 'c' 不直接产生图形，跳过。

    Args:
        page: PyMuPDF Page 对象。
        ignore_white: 是否跳过白色/无色图形。

    Returns:
        Box 列表，每个对应一个矢量图形的外接矩形（宽高均 >0）。

    算法：
        遍历 page.get_drawings() 返回的每个路径项：
        1. 判断类型是否包含填充 ('f') 或描边 ('s')，若两者皆无（纯裁剪 'c'）则跳过。
        2. 若 ignore_white=True，调用 _is_white_graphic 判断该路径是否应被跳过（白色填充/描边）。
        3. 取路径的 rect 字段，若宽高 >0 则转为 Box 加入结果。
    """
    boxes = []
    for item in page.get_drawings():
        kind = item.get("type", "")
        has_fill = "f" in kind
        has_stroke = "s" in kind
        if not has_fill and not has_stroke:
            continue  # clip-only 项
        # 白图过滤：仅当涉及的维度（fill/stroke）都是白/空色才跳过，语义照 addLinePath。
        # 注意 PyMuPDF 的 get_drawings() 里描边色在 key "color"（无 "stroke" key），fill 仍是 "fill"
        if ignore_white and _is_white_graphic(has_fill, has_stroke, item.get("fill"), item.get("color")):
            continue
        rect = item["rect"]
        b = Box(rect.x0, rect.y0, rect.x1, rect.y1)
        # 零宽/零高（纯线）排除：GraphicBBDetector 要求 width>0 && height>0
        if b.width > 0 and b.height > 0:
            boxes.append(b)
    return boxes


def _is_white_graphic(has_fill: bool, has_stroke: bool, fill, stroke) -> bool:
    """该图形是否按白图规则跳过（照 GraphicBBDetector 的 addLinePath 短路语义）。

    未填充/未描边的维度不参与判断（短路）：
        - 纯填充（has_fill=True, has_stroke=False）：仅当 fill 为白才跳。
        - 纯描边（has_fill=False, has_stroke=True）：仅当 stroke 为白才跳。
        - 既填又描：两者皆白才跳。
    返回 True 表示应跳过。

    Args:
        has_fill: 是否包含填充。
        has_stroke: 是否包含描边。
        fill: 填充颜色（RGB/RGBA 元组或 None）。
        stroke: 描边颜色（key "color" 的值，RGB/RGBA 元组或 None）。

    Returns:
        True 若该图形应被跳过（白图），否则 False。
    """
    if has_stroke and not _is_white(stroke):
        return False
    if has_fill and not _is_white(fill):
        return False
    return True


def _is_white(color) -> bool:
    """fill/stroke 颜色是否白（照 isWhite：非 pattern 且 RGB 全 1.0）。

    None（无颜色，等价 EmptyPattern）视为白；fill/stroke 是 3 或 4 元组，带 alpha 时取前三位。
    pattern 填充在 PyMuPDF get_drawings 中不表现为颜色元组（不暴露 pattern 属性），
    按「无颜色」处理——这是相对 PDFBox 的本移植近似。

    Args:
        color: 颜色值（None 或 (r,g,b) 或 (r,g,b,a) 元组，各分量 0~1）。

    Returns:
        True 若颜色为白色或无色。
    """
    if color is None:
        return True
    return all(c >= 1.0 for c in color[:3])


def _raster_graphics(page) -> list[Box]:
    """光栅图：get_image_info(xrefs=True) 每个图实例的 bbox → Box。

    对应 GraphicBBDetector.drawImage（图像也进图形区，不做白过滤——图像本身不是纯色）。
    get_image_info 每个「放置位置」一条记录，同一 xref 复用多次会各自成框。

    Args:
        page: PyMuPDF Page 对象。

    Returns:
        Box 列表，每个对应一个光栅图像的放置位置。
    """
    boxes = []
    for item in page.get_image_info(xrefs=True):
        x0, y0, x1, y1 = item["bbox"]
        b = Box(x0, y0, x1, y1)
        if b.width > 0 and b.height > 0:
            boxes.append(b)
    return boxes


def _is_side_panel(b: Box, bounds: Box) -> bool:
    """是否整页一侧的侧栏/色带（GraphicsExtractor 的 sidePanel 启发式）。

    高度占满页面（差 <1pt）且贴着页面左缘（x1<1）或右缘（x2 距右缘 <1pt）。
    这类图形通常是侧边装饰/索引而非图，归入 nonFigureGraphics。

    Args:
        b: 待判断的图形框。
        bounds: 页面边界。

    Returns:
        True 若 b 是侧栏装饰，否则 False。
    """
    return abs(b.height - bounds.height) < 1.0 and (b.x1 < 1.0 or abs(b.x2 - bounds.x2) < 1.0)


def _merge_boxes(boxes: list[Box], tol: float) -> list[Box]:
    """相交聚类：不断合并相交（容差 tol 内）的框直到两两不相交。照 Box.scala mergeBoxes。

    返回的框彼此至少相距 tol 的曼哈顿距离（tol 可为负）。迭代地挑一个与别的框相交的框，
    用其最小外接矩形替换相交的那一组，直到没有可再合并的。

    Args:
        boxes: 待聚类的 Box 列表。
        tol: 相交容差（正值允许间隙，负值要求重叠）。

    Returns:
        合并后的 Box 列表，任意两个框之间在 tol 容差下不相交。

    算法（与 Scala 的 mergeBoxes 一致）：
        1. 设置 current = boxes, found = True。
        2. 外层循环：while found:
             a. found = False, checked = [current[0]], unchecked = current[1:]。
             b. 内层循环：while not found and unchecked:
                  - head = checked[0]（基准框）
                  - 将 unchecked 分为 intersects（与 head 相交）和 non_intersects。
                  - 若 intersects 非空，则用 Box_container([head] + intersects) 合并成一个新框，
                    令 current = non_intersects + [new_box] + checked[1:]，found = True。
                    然后跳出内层循环，重新开始外层循环（因为列表已变化）。
                  - 若 intersects 为空，则将 unchecked[0] 移到 checked 前面，继续检查下一个基准。
             c. 若内层循环结束时未找到任何相交组，则 found 保持 False，循环终止。
        3. 返回 current（此时任意两框不相交）。
    """
    if not boxes:
        return boxes
    current = list(boxes)
    found = True
    while found:
        found = False
        checked = [current[0]]   # 已检查过的框；checked[0] 是当前合并的基准
        unchecked = current[1:]
        while not found and unchecked:
            head = checked[0]
            # 把剩余框按是否与基准相交分成两拨（partition 保持相对顺序）
            intersects = []
            non_intersects = []
            for b in unchecked:
                (intersects if b.intersects(head, tol) else non_intersects).append(b)
            if intersects:
                # 用基准 + 所有相交框的外接矩形替换这一组，重新进入外层循环
                new_box = Box_container([head] + intersects)
                current = non_intersects + [new_box] + checked[1:]
                found = True
            else:
                # 当前基准与所有剩余框都不相交，将其移入已检查列表，换下一个基准
                checked = [unchecked[0]] + checked
                unchecked = unchecked[1:]
    return current