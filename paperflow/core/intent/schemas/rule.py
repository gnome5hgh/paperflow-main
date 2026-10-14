"""规则层的一条高精度匹配模式。"""
import re
from dataclasses import dataclass

__all__ = ["Rule"]


@dataclass(frozen=True)
class Rule:
    """规则层的一条高精度模式。

    Attributes:
        intent: str，命中后判定的类别
        pattern: re.Pattern，已编译的模式
        raw: str，模式原文（报错与查重时用）
        note: str，这条规则为什么算高精度（给维护者看的判据）
    """

    intent: str
    pattern: re.Pattern[str]
    raw: str
    note: str = ""
