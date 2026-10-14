"""删除一条引用的结局。"""

from enum import StrEnum

__all__ = ["RemoveOutcome"]


class RemoveOutcome(StrEnum):
    """删除一条引用的结局。

    多条命中时不猜（误删是单向的），返回 AMBIGUOUS 连同候选交用户选择。
    """

    REMOVED = "removed"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
