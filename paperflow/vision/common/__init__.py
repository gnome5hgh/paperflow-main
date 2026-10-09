"""vision 域共用几何原语——被多个子包横向消费。

`geometry.py` 提供 Box / Word / Line / Paragraph：解析各步（文本、布局、图注、图形）、
检测各步（区域分类、图定位）、数据模型与渲染都以它为基本单位，故独立成包避免子包间
互相依赖。它不依赖 vision 的其他任何子包。

本包只做再导出，消费方从 `paperflow.vision.common` 取。
"""

from .geometry import (
    Box,
    Box_container,
    Box_crop,
    Line,
    Paragraph,
    Position,
    Word,
    find_empty_horizontal_blocks,
)

__all__ = [
    "Box", "Box_container", "Box_crop", "Line", "Paragraph", "Position", "Word",
    "find_empty_horizontal_blocks",
]
