# paperflow/core/intent/__init__.py
"""意图识别（可选预处理层）的统一导出点。

集成缝是 `IntentService`（Agent 只持一个可选的它，`None` 即「关」）；类别与规则
来自知识库 `data/intent/`，由 `taxonomy.load_taxonomy` 装载并做 fail-closed 校验。
"""
from paperflow.core.intent.constants import (
    INTENT_META, IntentCategory, IntentStep, IntentType,
)
from paperflow.core.intent.entities import extract_entities
from paperflow.core.intent.schemas import IntentOutput
from paperflow.core.intent.service import IntentService, Turn
from paperflow.core.intent.taxonomy import (
    INTENT_CLASSES, IntentClass, Rule, Taxonomy, TaxonomyError, load_taxonomy,
)

__all__ = [
    "INTENT_META", "INTENT_CLASSES", "IntentCategory", "IntentClass",
    "IntentOutput", "IntentService", "IntentStep", "IntentType",
    "Rule", "Taxonomy", "TaxonomyError", "Turn", "extract_entities", "load_taxonomy",
]
