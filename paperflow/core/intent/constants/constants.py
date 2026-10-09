"""意图元数据的单一真相源：类别 → 类别分组。

分组决定消费方式（业务类按需派发、系统类不派发领域角色），不决定派发顺序，也
不作门禁——意图只往 head 注入一条提示，派不派、派谁由 supervisor 自主决定。
"""
from .enums import IntentCategory, IntentType

__all__ = ["INTENT_META"]

#: 意图 → 类别分组。键必须与 IntentType 逐项一致（枚举 = 契约 = 实现集）。
INTENT_META: dict[IntentType, IntentCategory] = {
    IntentType.PAPER: IntentCategory.BUSINESS,
    IntentType.NOTE: IntentCategory.BUSINESS,
    IntentType.RESEARCH: IntentCategory.BUSINESS,
    IntentType.CITATION: IntentCategory.BUSINESS,
    IntentType.INDEX: IntentCategory.BUSINESS,
    IntentType.MEMORY: IntentCategory.BUSINESS,
    IntentType.QUESTION: IntentCategory.BUSINESS,
    IntentType.CHITCHAT: IntentCategory.SYSTEM,
    IntentType.OUT_OF_SCOPE: IntentCategory.SYSTEM,
    IntentType.HELP: IntentCategory.SYSTEM,
    IntentType.FEEDBACK: IntentCategory.SYSTEM,
}
