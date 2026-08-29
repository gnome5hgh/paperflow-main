"""视觉分析子包的数据模型：PDF 提取出的图表对象 + 视觉模型的结构化分析。"""
from dataclasses import dataclass

from pydantic import BaseModel


@dataclass
class Figure:
    """从 PDF 提取出的一个图表对象。

    Attributes:
        number: 图号（图注解析出的数字）。
        caption: 图注文本。
        page: 所在页码（1 起）。
        image_bytes: 图片字节（栅格原始格式或渲染 PNG）。
        mime: 图片 MIME（"image/png"/"image/jpeg"），用于 base64 data URL。
    """

    number: int
    caption: str
    page: int
    image_bytes: bytes
    mime: str


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
