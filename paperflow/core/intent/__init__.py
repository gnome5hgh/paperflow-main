# paperflow/core/intent/__init__.py
"""意图识别（可选预处理层）的统一导出点。

目录按角色分层：`constants/`（枚举 + 类别词汇/元数据/KB 路径）、`schemas/`（产出契约）、
`rules/`（规则层：实体抽取 + 知识库装载校验）、`services/`（判定服务客户端 + 集成缝）。

集成缝是 `IntentService`（Agent 只持一个可选的它，`None` 即「关」）；类别与规则
来自知识库 `.paperflow/intent/`，由 `rules.taxonomy.load_taxonomy` 装载并做 fail-closed 校验。
"""
from paperflow.core.intent.constants import INTENT_CLASSES, IntentStep, IntentType
from paperflow.core.intent.rules.entities import extract_entities
from paperflow.core.intent.rules.taxonomy import (
    IntentClass, Rule, Taxonomy, TaxonomyError, load_taxonomy,
)
from paperflow.core.intent.schemas import IntentOutput
from paperflow.core.intent.services.jev import JevClient, JevDecision, JevUnavailable
from paperflow.core.intent.services.service import IntentService, Turn

__all__ = [
    "INTENT_CLASSES", "IntentClass",
    "IntentOutput", "IntentService", "IntentStep", "IntentType",
    "JevClient", "JevDecision", "JevUnavailable",
    "Rule", "Taxonomy", "TaxonomyError", "Turn", "extract_entities", "load_taxonomy",
]
