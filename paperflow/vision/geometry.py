"""几何模型：Box/Word/Line/Paragraph（pdffigures2 的 Box.scala + Paragraph.scala 移植）。

后续的文本、布局、图注、图形、区域分类、图检测组件都以本模块为基本几何单位。
坐标约定：x 向右增大、y 向下增大，Box(x1, y1, x2, y2) 的 (x1, y1) 为左上角、
(x2, y2) 为右下角，均为闭区间。
"""
from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class Position:
    """单个字符/词的排版信息（对应 PDFBox TextPosition 的关键字段）。

    Attributes:
        font_size: 字号。
        font_name: 字体名。
    """

    font_size: float
    font_name: str


@dataclass(frozen=True)
class Word:
    """一个词：文本 + 包围盒 + 其各字符的排版信息列表。"""

    text: str
    boundary: Box
    positions: list[Position]

    def __post_init__(self) -> None:
        # 与 Paragraph.scala 一致：词必须至少有一个字符位置，空词没有意义
        if not self.positions:
            raise ValueError("word 必须有非空的 positions")


@dataclass(frozen=True)
class Line:
    """一行词：词列表 + 整行包围盒 + 是否水平（brief 接口显式传入，不做推导）。"""

    words: list[Word]
    boundary: Box
    is_horizontal: bool

    def __post_init__(self) -> None:
        if not self.words:
            raise ValueError("line 必须有非空的 words")

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class Paragraph:
    """一段：行列表 + 段落包围盒（= 所有行 boundary 的最小外接矩形）。

    与 Paragraph.scala 的 ParagraphContainer 语义一致：boundary 由调用方用
    Box_container 对各行 boundary 求并集后传入。
    """

    lines: list[Line]
    boundary: Box

    def __post_init__(self) -> None:
        if not self.lines:
            raise ValueError("paragraph 必须有非空的 lines")

    @property
    def text(self) -> str:
        return " ".join(l.text for l in self.lines)


@dataclass(frozen=True)
class Box:
    """轴对齐矩形：要求 x1<=x2 且 y1<=y2。

    明确允许零宽/零高——PDFBox 对合法文本片段也可能给出零面积边界，不能当非法拒绝。
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        if self.x1 > self.x2 or self.y1 > self.y2:
            raise ValueError("Box 要求 x1<=x2 且 y1<=y2（宽高不能为负）")

    @property
    def width(self) -> float:
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        return self.y2 - self.y1

    @property
    def xCenter(self) -> float:
        return (self.x2 + self.x1) / 2

    @property
    def yCenter(self) -> float:
        return (self.y2 + self.y1) / 2

    @property
    def area(self) -> float:
        return self.width * self.height

    def intersects(self, other: "Box", margin: float = 0.0) -> bool:
        """是否与 other 相交（margin 容差）。照抄 Box.scala。

        margin 把 other 向四周扩 margin 后再判重叠：正 margin 允许隔着空隙也算相交，
        负 margin 要求两侧真正重叠 |margin| 以上（边界接触不算）。
        """
        return not (
            self.x2 < other.x1 - margin
            or self.x1 > other.x2 + margin
            or self.y2 < other.y1 - margin
            or self.y1 > other.y2 + margin
        )

    def intersectRegion(self, other: "Box") -> "Box | None":
        """两框的交叠区域；不相交返回 None。"""
        if not self.intersects(other):
            return None
        return Box(
            max(self.x1, other.x1),
            max(self.y1, other.y1),
            min(self.x2, other.x2),
            min(self.y2, other.y2),
        )

    def intersectArea(self, other: "Box") -> float:
        """与 other 的交叠面积；不相交为 0。"""
        overlap = self.intersectRegion(other)
        return overlap.area if overlap is not None else 0.0

    def contains(self, other: "Box", margin: float = 0.0) -> bool:
        """other 是否在 self 内（other 向外扩 margin 后仍不越界即算含）。"""
        return (
            self.x1 <= other.x1 + margin
            and self.y1 <= other.y1 + margin
            and self.x2 >= other.x2 - margin
            and self.y2 >= other.y2 - margin
        )

    def container(self, other: "Box") -> "Box":
        """与 other 的最小外接矩形（并集）。"""
        return Box(
            min(self.x1, other.x1),
            min(self.y1, other.y1),
            max(self.x2, other.x2),
            max(self.y2, other.y2),
        )

    def copy(self, **kw) -> "Box":
        """返回改字段后的新 Box（frozen 实例不能直接改，用 replace 换新）。"""
        return replace(self, **kw)


def Box_container(boxes: list[Box]) -> Box:
    """所有 box 的最小外接矩形；空列表报错（对应 Scala 的 require）。"""
    if not boxes:
        raise ValueError("Box_container 不能对空列表求并集")
    return Box(
        min(b.x1 for b in boxes),
        min(b.y1 for b in boxes),
        max(b.x2 for b in boxes),
        max(b.y2 for b in boxes),
    )


def Box_crop(box: Box, boxes: list[Box], margin: float = 0.0) -> "Box | None":
    """把 box 从四边向里收，收到紧贴所有（margin 容差内）相交的内容。

    语义照抄 Box.scala 的 crop：返回仍与 boxes「在相同位置相交」的尽可能小的 box，
    即内容在 box 内的外接边界；若 box 内没有任何内容相交则返回 None。
    margin 为相交容差（同 intersects），与内容共享边界视为相交与否由此容差决定。
    """
    shrink_left = box.width
    shrink_right = box.width
    shrink_up = box.height
    shrink_down = box.height
    found_any = False
    for other in boxes:
        if other.intersects(box, margin):
            shrink_left = min(shrink_left, other.x1 - box.x1)
            shrink_right = min(shrink_right, box.x2 - other.x2)
            shrink_up = min(shrink_up, box.y2 - other.y2)
            shrink_down = min(shrink_down, other.y1 - box.y1)
            found_any = True
    if not found_any:
        return None
    return Box(
        box.x1 + max(shrink_left, 0),
        box.y1 + max(shrink_down, 0),
        box.x2 - max(shrink_right, 0),
        box.y2 - max(shrink_up, 0),
    )


def find_empty_horizontal_blocks(region: Box, content: list[Box]) -> list[Box]:
    """region 内被 content 上下夹出的水平空白带。

    照抄 Box.scala 的 findEmptyHorizontalBlocks：从 region 出发，对每个 content 框
    把与其相交的空白带裁成一块或多块。产出等宽（保持 region 的 x1/x2）、极大扩展、
    彼此互不相交、也不与 content 相交的空白带。
    """
    empty_blocks = [region]
    for content_box in content:
        new_blocks = []
        for empty_region in empty_blocks:
            if not content_box.intersects(empty_region):
                new_blocks.append(empty_region)
            elif content_box.y1 <= empty_region.y1:
                # content 从上方盖住空白带：只在 content 底缘下方留缝；
                # 盖到空白带底缘则整条被吃掉
                if content_box.y2 < empty_region.y2:
                    new_blocks.append(empty_region.copy(y1=content_box.y2))
            elif content_box.y2 >= empty_region.y2:
                # content 从下方盖住空白带：只在 content 顶缘上方留缝；
                # 盖到空白带顶缘则整条被吃掉
                if content_box.y1 > empty_region.y1:
                    new_blocks.append(empty_region.copy(y2=content_box.y1))
            else:
                # content 横穿空白带中间：裁成上下两块
                new_blocks.append(empty_region.copy(y2=content_box.y1))
                new_blocks.append(empty_region.copy(y1=content_box.y2))
        empty_blocks = new_blocks
    return empty_blocks
