"""意图识别的词汇与映射（单一真相源）。

枚举在 `enums.py`，类别词汇/元数据映射/知识库路径在 `constants.py`；本包只做再导出，
消费方一律从 `paperflow.core.intent.constants` 取。
"""

from .constants import (
    INTENT_CLASSES, INTENT_KB_DIR, INTENT_META, RULES_PATH, TAXONOMY_PATH,
)
from .enums import IntentCategory, IntentStep, IntentType

__all__ = ["INTENT_CLASSES", "INTENT_KB_DIR", "INTENT_META", "RULES_PATH",
           "TAXONOMY_PATH", "IntentCategory", "IntentStep", "IntentType"]
