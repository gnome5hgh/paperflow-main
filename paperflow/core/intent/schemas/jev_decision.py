"""判定服务（Jev 层）一次判定的结果。"""
from dataclasses import dataclass

__all__ = ["JevDecision"]


@dataclass(frozen=True)
class JevDecision:
    """一次判定的结果。

    Attributes:
        choice: str，模型选中的类别名（调用方负责校验它是已知类别）。
        confidence: float | None，选中项的概率。**没有判定消费者**，只作展示与排查。
    """

    choice: str
    confidence: float | None
