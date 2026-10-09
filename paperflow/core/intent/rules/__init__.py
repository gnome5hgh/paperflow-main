"""规则层：确定性实体抽取 + 知识库装载与模式匹配。

规则层是判定链的第一层——命中即定类、不命中即放行，**永不猜测**。它的覆盖面故意窄：
只收「说了这句基本不可能是别的意思」的说法，因为误命中的代价高于多跑一次判定服务。
"""
from paperflow.core.intent.rules.entities import extract_entities
from paperflow.core.intent.rules.taxonomy import (
    IntentClass, Rule, Taxonomy, TaxonomyError, load_taxonomy,
)

__all__ = ["IntentClass", "Rule", "Taxonomy", "TaxonomyError",
           "extract_entities", "load_taxonomy"]
