"""vision 域的跨模块词汇。

两类：取值枚举（`enums.py`，取值即 pdffigures2 的英文原型，随解析产物进入图检测、
区域分类与渲染，也决定媒体块按「图」还是「表」入库）与结构常量（`constants.py`，
跨文件共享的分桶粒度）。本包只做再导出，消费方一律从 `paperflow.vision.constants` 取。
"""

from .constants import LINE_WIDTH_BUCKET_SIZE
from .enums import FigureType

__all__ = ["FigureType", "LINE_WIDTH_BUCKET_SIZE"]
