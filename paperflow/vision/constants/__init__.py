"""vision 域的跨模块词汇。

取值即 pdffigures2 的英文原型，随解析产物进入图检测、区域分类与渲染，也决定媒体块
按「图」还是「表」入库。枚举在 `enums.py`；本包只做再导出，消费方一律从
`paperflow.vision.constants` 取。
"""

from .enums import FigureType

__all__ = ["FigureType"]
