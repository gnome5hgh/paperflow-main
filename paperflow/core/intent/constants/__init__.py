"""意图识别的词汇与映射（单一真相源）。

枚举在 `enums.py`，类别分组映射在 `constants.py`；本包只做再导出，消费方一律
从 `paperflow.core.intent.constants` 取。
"""

from .constants import INTENT_META
from .enums import IntentCategory, IntentStep, IntentType

__all__ = ["IntentType", "IntentCategory", "IntentStep", "INTENT_META"]
