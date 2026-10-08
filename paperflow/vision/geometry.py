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
    """一个词：文本 + 包围盒 + 其各字符的排版信息列表。

    Attributes:
        text: 词的文本。
        boundary: 词的外接矩形。
        positions: 每个字符的排版信息，长度应与词内字符数一致。
    """

    text: str
    boundary: Box
    positions: list[Position]

    def __post_init__(self) -> None:
        """校验词至少有一个字符位置（空词无意义）。"""
        # 与 Paragraph.scala 一致：词必须至少有一个字符位置，空词没有意义
        if not self.positions:
            raise ValueError("word 必须有非空的 positions")


@dataclass(frozen=True)
class Line:
    """一行词：词列表 + 整行包围盒 + 是否水平（由调用方显式传入，不做推导）。

    Attributes:
        words: 该行包含的词（按阅读顺序）。
        boundary: 整行的外接矩形。
        is_horizontal: 行是否为水平方向（非旋转/竖直文本）。
    """

    words: list[Word]
    boundary: Box
    is_horizontal: bool

    def __post_init__(self) -> None:
        """校验行至少含一个词。"""
        if not self.words:
            raise ValueError("line 必须有非空的 words")

    @property
    def text(self) -> str:
        """整行文本（各词以空格连接）。"""
        return " ".join(w.text for w in self.words)


@dataclass(frozen=True)
class Paragraph:
    """一段：行列表 + 段落包围盒（= 所有行 boundary 的最小外接矩形）。

    与 Paragraph.scala 的 ParagraphContainer 语义一致：boundary 由调用方用
    Box_container 对各行 boundary 求并集后传入。

    Attributes:
        lines: 该段包含的行（按阅读顺序）。
        boundary: 段落的外接矩形。
    """

    lines: list[Line]
    boundary: Box

    def __post_init__(self) -> None:
        """校验段至少含一行。"""
        if not self.lines:
            raise ValueError("paragraph 必须有非空的 lines")

    @property
    def text(self) -> str:
        """整段文本（各行以空格连接）。"""
        return " ".join(l.text for l in self.lines)


@dataclass(frozen=True)
class Box:
    """轴对齐矩形：要求 x1<=x2 且 y1<=y2。

    明确允许零宽/零高——PDFBox 对合法文本片段也可能给出零面积边界，不能当非法拒绝。

    Attributes:
        x1: float，左边界（PDF 点）
        y1: float，上边界
        x2: float，右边界
        y2: float，下边界
        width: float，派生：x2 - x1
        height: float，派生：y2 - y1
        xCenter: float，派生：水平中心
        yCenter: float，派生：垂直中心
        area: float，派生：宽 × 高
    """

    x1: float
    y1: float
    x2: float
    y2: float

    def __post_init__(self) -> None:
        """校验宽高非负（x1<=x2 且 y1<=y2；零宽零高合法）。"""
        if self.x1 > self.x2 or self.y1 > self.y2:
            raise ValueError("Box 要求 x1<=x2 且 y1<=y2（宽高不能为负）")

    @property
    def width(self) -> float:
        """矩形宽度（x2 - x1）。"""
        return self.x2 - self.x1

    @property
    def height(self) -> float:
        """矩形高度（y2 - y1）。"""
        return self.y2 - self.y1

    @property
    def xCenter(self) -> float:
        """水平中心坐标。"""
        return (self.x2 + self.x1) / 2

    @property
    def yCenter(self) -> float:
        """垂直中心坐标。"""
        return (self.y2 + self.y1) / 2

    @property
    def area(self) -> float:
        """矩形面积（宽 × 高）。"""
        return self.width * self.height

    def intersects(self, other: "Box", margin: float = 0.0) -> bool:
        """是否与 other 相交（margin 容差）。照抄 Box.scala。

        margin 把 other 向四周扩 margin 后再判重叠：正 margin 允许隔着空隙也算相交，
        负 margin 要求两侧真正重叠 |margin| 以上（边界接触不算）。

        Args:
            other: 另一个矩形框。
            margin: 扩展容差。正 margin 表示允许两框之间有空隙仍算相交（即扩大 other）；
                    负 margin 要求两框必须相互覆盖至少 |margin| 距离（边界接触不算）。
        Returns:
            True 若相交，否则 False。
        """
        # 检查在 x 和 y 方向上是否都不分离：self.x2 < other.x1 - margin 等四个条件。
        return not (
            self.x2 < other.x1 - margin
            or self.x1 > other.x2 + margin
            or self.y2 < other.y1 - margin
            or self.y1 > other.y2 + margin
        )

    def intersectRegion(self, other: "Box") -> "Box | None":
        """两框的交叠区域；不相交返回 None。

        Args:
            other: 另一个矩形框。
        Returns:
            表示交叠区域的新 Box，或 None。
        """
        if not self.intersects(other):
            return None
        return Box(
            max(self.x1, other.x1),
            max(self.y1, other.y1),
            min(self.x2, other.x2),
            min(self.y2, other.y2),
        )

    def intersectArea(self, other: "Box") -> float:
        """与 other 的交叠面积；不相交为 0。

        Args:
            other: 另一个矩形框。
        Returns:
            交叠面积（浮点数）。
        """
        overlap = self.intersectRegion(other)
        return overlap.area if overlap is not None else 0.0

    def contains(self, other: "Box", margin: float = 0.0) -> bool:
        """判断 other 是否被当前框包含（允许容差 margin）。

        Args:
            other: 被检查的矩形框。
            margin: 容差，为正时允许 other 向外扩展 margin 仍算包含（即边界可略微超出）。
        Returns:
            True 若 other 在 self 内（含边界）.
        """
        return (
            self.x1 <= other.x1 + margin
            and self.y1 <= other.y1 + margin
            and self.x2 >= other.x2 - margin
            and self.y2 >= other.y2 - margin
        )

    def container(self, other: "Box") -> "Box":
        """返回包含当前框和 other 的最小外接矩形（并集）。

        Args:
            other: 另一个矩形框。
        Returns:
            一个新的 Box，其边界为两框的并集。
        """
        return Box(
            min(self.x1, other.x1),
            min(self.y1, other.y1),
            max(self.x2, other.x2),
            max(self.y2, other.y2),
        )

    def copy(self, **kw) -> "Box":
        """返回修改指定字段后的新 Box（因为 dataclass frozen 不可变，用 replace 换新）。

        Args:
            **kw: 要修改的字段名和值，如 x1=10, y1=20。
        Returns:
            新的 Box 实例。
        """
        return replace(self, **kw)


def Box_container(boxes: list[Box]) -> Box:
    """计算所有框的最小外接矩形（并集）。

    Args:
        boxes: 非空的 Box 列表。
    Returns:
        一个 Box，其边界包含所有输入框。
    Raise:
        ValueError: 如果 boxes 为空。
    """
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

    算法:
        对每个内容框，计算它相对 box 四边的内缩量（左、右、上、下），取各方向最小内缩，
        然后应用这些内缩量（非负）得到收缩后的框。

    Args:
        box: 待裁剪的原始矩形。
        boxes: 内容框列表（通常为图形、文本等）。
        margin: 与内容框相交的容差，同 Box.intersects。
    Returns:
        裁剪后的 Box，或 None（若 box 与任何内容框都不相交）。
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

    算法:
        初始空白带为 region。遍历每个 content 框，若与某个空白带相交，则根据 content
        框的纵向位置，将空白带切分为上方、下方或中间两块（或被覆盖掉），从而逐步细化。

    Args:
        region: 搜索区域（通常为一个候选图区域）。
        content: 内容框列表（如文本、图形等），用于界定空白带的边界。
    Returns:
        一个 Box 列表，每个代表一个水平方向的空白带状区域（宽度与 region 相同，
        高度由内容框夹出），它们互不重叠且不包含任何 content 框。
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
