"""视觉分析子包：PDF 图表提取 + 视觉模型看图分析。

包根只做再导出：常量、数据模型、共用几何、服务、检测、解析各有一个子包，按角色划分
（与 `rag/` 同构）。
"""
from paperflow.vision.schemas import Figure, FigureAnalysis
from paperflow.vision.services import FigureAnalyzer, FigureExtractor, render_figure

__all__ = [
    "FigureExtractor", "FigureAnalyzer", "render_figure", "Figure", "FigureAnalysis",
]
