"""意图的元数据映射——类别/派发权限与中文短名。"""

from .enums import IntentCategory, IntentType

__all__ = ["INTENT_META", "INTENT_LABELS_ZH"]

# 意图 → (category, dispatch_allowed)——单一真相源。枚举=契约=实现集
# dispatch_allowed=False 的意图由 spawn 门禁代码级拒绝派发
INTENT_META: dict[IntentType, tuple[IntentCategory, bool]] = {
    IntentType.SET_RESEARCH_TOPIC: (IntentCategory.BUSINESS, False), # set_research_topic 是业务但非派发——记录+引导
    IntentType.MENU_SELECTION:     (IntentCategory.DIALOGUE, True), # menu_selection 是对话管理但派发——选择动作，派发权在 supervisor 对照菜单
    IntentType.SEARCH_PAPER:       (IntentCategory.BUSINESS, True),
    IntentType.ASK_QUESTION:       (IntentCategory.BUSINESS, True),
    IntentType.GENERATE_NOTE:      (IntentCategory.BUSINESS, True),
    IntentType.RESEARCH_DISCOVERY: (IntentCategory.BUSINESS, True),
    IntentType.ANALYZE_PAPER:      (IntentCategory.BUSINESS, True),
    IntentType.MANAGE_MEMORY:      (IntentCategory.BUSINESS, True),
    IntentType.MANAGE_CITATIONS:  (IntentCategory.BUSINESS, True),
    IntentType.CHITCHAT:           (IntentCategory.SYSTEM, False),
    IntentType.OUT_OF_SCOPE:       (IntentCategory.SYSTEM, False),
    IntentType.HELP:               (IntentCategory.SYSTEM, False),
    IntentType.FEEDBACK:           (IntentCategory.SYSTEM, False),
    IntentType.UNCLASSIFIED:       (IntentCategory.SYSTEM, False),
}


#: 意图 → 中文短标签。用于两类面向用户的场合：澄清模板合成兜底问题（枚举英文值
#: 用户看不懂）、日志/展示层。只求自足易懂，不复述枚举注释里的完整判据。
INTENT_LABELS_ZH: dict[IntentType, str] = {
    IntentType.SET_RESEARCH_TOPIC: "设定/切换研究方向",
    IntentType.MENU_SELECTION:     "菜单选项选择",
    IntentType.SEARCH_PAPER:       "搜索论文",
    IntentType.ASK_QUESTION:       "论文问答",
    IntentType.GENERATE_NOTE:      "撰写笔记",
    IntentType.RESEARCH_DISCOVERY: "选题发现",
    IntentType.ANALYZE_PAPER:      "精读分析",
    IntentType.MANAGE_MEMORY:      "记忆/清单管理",
    IntentType.MANAGE_CITATIONS:  "引用库管理",
    IntentType.CHITCHAT:           "闲聊",
    IntentType.OUT_OF_SCOPE:       "超出能力范围的请求",
    IntentType.HELP:               "使用帮助",
    IntentType.FEEDBACK:           "结果反馈",
    IntentType.UNCLASSIFIED:       "未分类",
}
