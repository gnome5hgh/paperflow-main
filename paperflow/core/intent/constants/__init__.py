"""意图识别的词汇与映射（单一真相源）。

枚举在 `enums.py`，意图元数据映射在 `constants.py`；本包只做再导出，消费方
一律从 `paperflow.core.intent.constants` 取。意图值即路由名，对应 routes.yaml。
"""

from .constants import INTENT_LABELS_ZH, INTENT_META
from .enums import IntentCategory, IntentStep, IntentType

__all__ = ["IntentType", "IntentCategory", "IntentStep",
           "INTENT_META", "INTENT_LABELS_ZH"]
