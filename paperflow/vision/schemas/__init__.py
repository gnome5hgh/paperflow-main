"""vision 域的数据模型（跨段共享）。

`figure.py` 是提取管线的产物、`analysis.py` 是视觉模型的产物，但两者都是消费方
（视觉分析器、analyze_figures 工具、媒体块构造）直接读取的载体，故集中声明、与实现
解耦——两段式管线正是以这里的模型衔接。本包只做再导出，消费方从
`paperflow.vision.schemas` 取。
"""

from .analysis import FigureAnalysis
from .figure import Figure

__all__ = ["Figure", "FigureAnalysis"]
