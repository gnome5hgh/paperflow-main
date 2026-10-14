"""知识库里的一个意图类别：判定口径 + 示例句。"""
from dataclasses import dataclass

__all__ = ["IntentClass"]


@dataclass(frozen=True)
class IntentClass:
    """一个类别：判定口径 + 示例句。

    Attributes:
        name: str，类别名（= INTENT_CLASSES 之一）
        description: str，判定口径，写清「是什么 / 不包什么」
        examples: tuple[str, ...]，示例句（与描述一起进判定模型的 criteria）
    """

    name: str
    description: str
    examples: tuple[str, ...]
