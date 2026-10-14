"""视觉模型看图的结构化分析结果模型。"""

from pydantic import BaseModel


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
