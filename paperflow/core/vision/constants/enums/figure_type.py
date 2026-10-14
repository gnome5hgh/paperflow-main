"""图注类型。"""

from enum import StrEnum

__all__ = ["FigureType"]


class FigureType(StrEnum):
    """图注类型：图 / 表（对应 pdffigures2 的 FigureType 枚举，取值即英文原型）。"""

    Figure = "Figure"
    Table = "Table"
