"""视觉分析子包的数据模型：PDF 提取出的图表对象 + 视觉模型的结构化分析。"""

from dataclasses import dataclass
from pydantic import BaseModel
from paperflow.vision.geometry import Box


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
        fig_type: 图注类型，目前管线仅产出 "Figure"，保留字段用于扩展（如 "Table"）。
        image_text: 图区域内识别出的图内文本，所有词以空格拼接（可能为空）。
        caption_boundary: 图注段落的包围盒（Box），可能为 None（仅当图注未扩展成功）。
        region_boundary: 检测出的图区域包围盒（Box），用于渲染和后续分析。

    边界条件：
        - number 为 0 并不代表图号真的是 0，而是表示 name 无法解析为整数。
        - image_bytes 总为非空字节（管线保证）。
        - caption_boundary 和 region_boundary 可为 None，但通常在成功检测后均非 None。
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
    """视觉模型对单张图的结构化分析结果（Pydantic 模型）。

    该模型定义了从视觉 LLM 提取的九个分析维度，所有字段（除 number 外）均有默认空字符串，
    以保证模型输出即使缺失某些字段也能通过 Pydantic 校验。配合 StructuredOutput 的 fallback
    机制，可确保始终返回合法的 FigureAnalysis 实例。

    Attributes:
        number: 图号整数（必需，通常由 fallback 填充）。
        caption: 图注文本（默认空串）。
        insight: 这张图在表达什么（一句话核心概括）。
        chart_type: 图表类型（如热力图、柱状图、流程图等）。
        notable: 新颖/优秀之处（视觉上的突出亮点）。
        applicable_scenarios: 适用场景（该图适合解决什么问题）。
        tool_guess: 制作工具推测（如 Matplotlib、TikZ、Adobe Illustrator 等）。
        color_scheme: 配色方案描述（如冷暖色调、对比度等）。
        layout_tips: 布局/标注技巧（图中布局或标注方面的可取之处）。
        font_annotation: 字体/标注规范（字体选择、字号、标注风格等）。

    注意：
        - 所有字符串字段默认为空，便于 fallback 构造。
        - 实际分析时，模型应尽可能填充所有字段，但即使全部为空，对象仍有效。
    """
    number: int
    caption: str = ""
    insight: str = ""
    chart_type: str = ""
    notable: str = ""
    applicable_scenarios: str = ""
    tool_guess: str = ""
    color_scheme: str = ""
    layout_tips: str = ""
    font_annotation: str = ""