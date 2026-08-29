"""视觉分析子包的数据模型：PDF 提取出的图表对象 + 视觉模型的结构化分析。"""
from dataclasses import dataclass

from pydantic import BaseModel

from paperflow.vision.geometry import Box


@dataclass
class Figure:
    """从 PDF 提取出的一个图表对象（FigureDetector 占位 dict 的结构化投影）。

    Attributes:
        number: 图号（从 name 解析出的数字；name 解析失败归 0，如两段式图号 "3.1"）。
        caption: 图注文本（= caption_text 的别名）。
        page: 所在页码（0 起，与 text_extractor 的 Page.page_number 一致）。
        image_bytes: 图片字节（渲染出的 PNG）。
        mime: 图片 MIME（"image/png"），用于 base64 data URL。
        name: 图号原始字符串（如 "1" / "3.1"），透传自图注。
        fig_type: 图注类型（"Figure"/"Table"）；管线只产出 Figure。
        image_text: 图区域内的图内文本（词以空格拼接）。
        caption_boundary: 图注段落包围盒。
        region_boundary: 检测出的图区域包围盒。
    """

    number: int
    caption: str
    page: int
    image_bytes: bytes
    mime: str
    name: str = ""
    fig_type: str = "Figure"
    image_text: str = ""
    caption_boundary: Box | None = None
    region_boundary: Box | None = None


class FigureAnalysis(BaseModel):
    """视觉模型对单张图的结构化分析（字段即 §5 逐图段的素材）。

    字段全部给默认空串：视觉模型缺字段不崩校验，fallback 也能构造出可解析实例。
    """

    number: int
    caption: str = ""
    insight: str = ""                    # 这张图在表达什么（一句话核心）
    chart_type: str = ""                 # 图表类型（热力图/柱状图/流程图…）
    notable: str = ""                    # 新颖/优秀之处
    applicable_scenarios: str = ""       # 适用场景
    tool_guess: str = ""                 # 制作工具推测
    color_scheme: str = ""               # 配色方案
    layout_tips: str = ""                # 布局/标注技巧
    font_annotation: str = ""            # 字体/标注规范
