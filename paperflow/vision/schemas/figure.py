"""从 PDF 提取出的图表对象（图与表）。

提取管线的最终产出。两类消费方各取所需：视觉分析器与 analyze_figures 工具用
number/caption/image_bytes/mime 看图；索引侧造媒体块只用 caption/image_words/
region_boundary/page（不要图像，故不渲染）。
"""

from dataclasses import dataclass

from paperflow.vision.common.geometry import Box


@dataclass
class Figure:
    """从 PDF 提取出的一个图表对象（FigureDetector 占位 dict 的结构化投影）。

    此类是 FigureExtractor 管线的最终产出，包含图区域渲染后的像素数据及元信息。
    视觉分析器（FigureAnalyzer）主要使用 number、caption、image_bytes 和 mime 字段。

    Attributes:
        number: 图号整数。优先从 name 解析，若 name 不是纯数字（如 "3.1"）则解析失败归 0。
                注意：两段式图号目前不支持解析为整数，统一归 0。
        caption: 图注完整文本（与 caption_text 等价，便于下游使用）。
        page: 所在页码，0-based，与 text_extractor 返回的 Page.page_number 一致。
        image_bytes: 渲染出的图区域 PNG 字节，可直接用于 base64 编码或存储。
        mime: 图片 MIME 类型，固定为 "image/png"（目前仅支持 PNG 输出）。
        name: 图号原始字符串，透传自图注检测结果（如 "1"、"3.1"、"III" 等）。
        fig_type: 图注类型（"Figure" 或 "Table"），由管线按图注首词判定。
        image_words: 图区域内识别出的图内文本**词与包围盒**；表格重建要靠坐标分
            行列，所以连包围盒一起给出（只要文字的调用方自己 join）。
        caption_boundary: 图注段落的包围盒（Box），可能为 None（仅当图注未扩展成功）。
        region_boundary: 检测出的图区域包围盒（Box），用于渲染和后续分析。

    边界条件：
        - number 为 0 并不代表图号真的是 0，而是表示 name 无法解析为整数。
        - image_bytes 通常非空；调用方要求不渲染时（只要区域定位与区域文本）为
          空字节、mime 为空串。
        - caption_boundary 和 region_boundary 可为 None，但通常在成功检测后均非 None。
    """

    number: int
    caption: str
    page: int
    image_bytes: bytes
    mime: str
    name: str = ""
    fig_type: str = "Figure"
    image_words: tuple[tuple[str, Box], ...] = ()
    caption_boundary: Box | None = None
    region_boundary: Box | None = None
