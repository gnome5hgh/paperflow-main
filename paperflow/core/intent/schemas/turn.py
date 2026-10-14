"""意图预处理的回传载体：一次 run 开头，意图层给 Agent 两样东西。"""
from dataclasses import dataclass

__all__ = ["Turn"]


@dataclass
class Turn:
    """一次 run 开头的意图预处理结果。

    Attributes:
        head_block: str | None，要注入的 INTENT 块文本；本轮没有判定结果时为 None
        task: str，可供使用的任务文本（判定不改写任务，原样返回）
    """

    head_block: str | None
    task: str
