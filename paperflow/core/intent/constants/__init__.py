"""意图识别的词汇与常量（单一真相源）。

枚举在 `enums.py`，类别词汇与知识库路径在 `constants.py`；本包只做再导出，
消费方一律从 `paperflow.core.intent.constants` 取。
"""

from .constants import INTENT_CLASSES, INTENT_KB_DIR, RULES_PATH, TAXONOMY_PATH
from .enums import IntentStep, IntentType

__all__ = ["INTENT_CLASSES", "INTENT_KB_DIR", "RULES_PATH", "TAXONOMY_PATH",
           "IntentStep", "IntentType"]
