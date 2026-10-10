"""章节实体：切块器的输入单元。

解析器把文档拆成章节（标题 + 正文 + 版面位置），切块器再按它决定块边界与重叠。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class Section:
    """切块器的输入单元：一个章节的标题、正文，以及它在原文档里的版面位置。

    Attributes:
        heading: 章节标题（可能为空——文档开头的无标题段）。
        text: 章节正文。
        positions: 该章节覆盖到的区域，每项为 ``(页, left, right, top, bottom)``。
            页从 1 起、坐标取整；笔记等无版面信息的来源为空元组。
    """

    heading: str
    text: str
    positions: tuple[tuple[int, int, int, int, int], ...] = ()
