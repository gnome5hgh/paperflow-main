"""由枚举派生的白名单与排序映射——不另立第二份取值表。"""

from .enums import RiskLevel, SideEffect

__all__ = ["SIDE_EFFECTS", "RISK_LEVELS", "RISK_ORDER"]

#: 可声明的副作用集合，side_effects 字段的值必须 ∈ 此集合
SIDE_EFFECTS = frozenset(m.value for m in SideEffect)

#: 合法风险等级集合，risk_level 字段的值必须 ∈ 此集合
RISK_LEVELS = frozenset(m.value for m in RiskLevel)

#: 风险等级 → 数值映射，供策略引擎比较风险大小
RISK_ORDER = {m.value: i for i, m in enumerate(RiskLevel)}
