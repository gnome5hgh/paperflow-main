"""渲染块 dto：本地 PDF 解析产出的一个 markdown 块及其页上位置。"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Block:
    """一个已渲染的 markdown 块（一段正文或一个标题行）及其在原页上的位置。

    Attributes:
        text: 块文本（标题行带 ``#`` 前缀；段落内部用换行连接）。
        page: 页码，1 起（PDF 页序）。
        left: 包围盒左边界（点，向下取整）。
        right: 包围盒右边界。
        top: 包围盒上边界。
        bottom: 包围盒下边界。

    边界条件：跨行合并的标题取其首行的页码，包围盒取各行的并集。
    """

    text: str
    page: int
    left: int
    right: int
    top: int
    bottom: int
