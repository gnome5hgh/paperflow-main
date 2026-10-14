"""本轮意图判定来自哪一层。"""
from enum import StrEnum

__all__ = ["IntentStep"]


class IntentStep(StrEnum):
    """本轮判定来自哪一层。

    Attributes:
        RULE: 规则层的确定性模式命中。
        JEV: 判定服务（结合对话史的模型判定）。
    """

    RULE = "rule"
    JEV = "jev"
