"""追问检测：判断当前输入是否承接上一轮意图。词表启发式，不依赖 LLM。

规则：出现承接标记（那/这/它/呢/然后/还有/再…）AND 无动作动词 AND 未引入新指代实体。
边界案例：
  "那 Figure 3 呢？"（引用上轮对象）→ 判定为追问，继承上轮意图
  "再下载一篇"（含新动作词"下载"）→ 不是追问，正常路由
  "这篇呢？"（"这篇"+量词指向列表新对象）→ 不是追问，正常路由
"""
import re

from paperflow.core.intent.schemas.intent import IntentType

#: 承接标记：出现即可能是追问（含叠词变体）
FOLLOWUP_MARKERS = frozenset({
    "那", "这", "它", "呢", "然后", "还有", "再", "继续", "接着",
    "然后呢", "还有呢", "那么",
})
#: 动作动词：含任一即视为新请求（不是纯追问），正常路由
ACTION_VERBS = frozenset({
    "搜索", "查找", "查", "找", "下载", "整理", "写", "生成",
    "阅读", "读", "回答", "解释", "分析", "总结", "列出", "显示",
})
#: "这篇/那篇"+量词 → 指向列表新对象，不算承接上轮对象
QUANTIFIER = re.compile(r"(?:这|那)(?:篇|个|本|份|些|条|张)")


def detect_followup(query: str, prev_intent: IntentType | None) -> bool:
    """判定 query 是否是对上轮意图的追问（是则继承 prev_intent）。

    三元判定缺一不可：① 有承接标记；② 无动作动词（有任一即视为新请求）；
    ③ 未引入新指代实体（"这篇/那个"+量词指向列表新对象，不算承接上轮对象）。
    prev_intent 为空（首轮）时直接判定不是追问。
    """
    if prev_intent is None:
        return False
    if not any(m in query for m in FOLLOWUP_MARKERS):
        return False
    if any(v in query for v in ACTION_VERBS):
        return False
    if QUANTIFIER.search(query):
        return False
    return True
