"""vision 域的服务层：两段式管线的两端与收尾。

`extractor.py` 是提取管线的编排（PDF → 图区候选）、`analyzer.py` 是视觉模型看图分析、
`renderer.py` 是管线最后一步把图区栅格化成 PNG（只被 extractor 调用，是管线的内部环节，
故归这里而非共用包）。三者都以 `schemas/` 的模型对外交接。

本包只做再导出，消费方从 `paperflow.vision.services` 取。
"""

from .analyzer import FigureAnalyzer
from .extractor import FigureExtractor
from .renderer import render_figure

__all__ = ["FigureAnalyzer", "FigureExtractor", "render_figure"]
