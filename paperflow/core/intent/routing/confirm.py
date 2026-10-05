# paperflow/core/intent/routing/confirm.py
"""意图确认原语——「用户从候选里选定意图」的格式化与解析。

澄清统一的落点（2026-10-04）：两条澄清通道（管线澄清 / agent 的 ask_user_question）
共用本模块——问题文本末尾由代码追加编号选项（format_intent_options），用户回复由
代码解析回意图（match_option_choice），命中即代码级写会话意图、不经路由器复判
（同一句话复判只会复现同一误判——路径污染死锁的机理）。未命中一律回退保守路径，
绝不猜测。
"""
import re

from paperflow.core.intent.constants import SHORT_REPLY_MAX_CHARS
from paperflow.core.intent.schemas.intent import INTENT_LABELS_ZH, IntentType

from paperflow.core.intent.routing.option_reply import OPTION_REPLY_RE

#: 短回复里的首个整数（用于「选2」「我要第 3 个」等非纯编号形态）。
#: 长度上限（SHORT_REPLY_MAX_CHARS）防长句里的年份/图号被误当选项号——确认回复天然是短语。
_NUMBER_RE = re.compile(r"[0-9０-９]+")


def format_intent_options(options: list[IntentType]) -> str:
    """把候选意图格式化为编号选择行，追加在澄清问题末尾。

    编号顺序即 match_option_choice 的解析顺序——两处必须同源（同一个列表按序
    展示、按序解析），调用方不得在展示与解析之间重排。
    """
    labels = [INTENT_LABELS_ZH.get(t, t.value) for t in options]
    return "请回复编号选择：" + " / ".join(
        f"{i + 1}) {label}" for i, label in enumerate(labels))


def match_option_choice(reply: str, options: list[IntentType]) -> IntentType | None:
    """把用户对澄清的回复解析成候选意图之一；解析不了返回 None（保守回退）。

    解析策略（从严到宽，全部确定性、无 LLM）：
      1. 纯编号（复用 option_reply 的正则：'1' '选项2' '第3个'）→ 按位取候选；
      2. 短回复里的首个整数（'选2' '我要第 3 个'）→ 同上，越界为 None；
      3. 精确匹配候选的中文短标签或枚举值（'精读分析' / 'analyze_paper'，
         ASCII 大小写不敏感）。
    不做子串猜测（「不是分析」也含「分析」）——猜错意图的代价比让用户再答一次高。

    Args:
        reply: 用户对澄清问题的原始回复。
        options: 候选意图列表（与 format_intent_options 展示顺序一致）。

    Returns:
        命中的候选意图；无法确定时 None。
    """
    if not reply or not options:
        return None
    text = reply.strip()

    # 1. 纯编号 → 按位取
    if OPTION_REPLY_RE.match(text):
        digits = re.sub(r"[０-９]", lambda m: chr(ord(m.group()) - 0xFEE0), text)
        n = int(_NUMBER_RE.search(digits).group())
        return options[n - 1] if 1 <= n <= len(options) else None

    # 2. 短回复里的首个整数（全角转半角后解析）
    if len(text) <= SHORT_REPLY_MAX_CHARS:
        normalized = re.sub(r"[０-９]", lambda m: chr(ord(m.group()) - 0xFEE0), text)
        if m := _NUMBER_RE.search(normalized):
            n = int(m.group())
            if 1 <= n <= len(options):
                return options[n - 1]

    # 3. 精确匹配中文短标签 / 枚举值
    lowered = text.lower()
    for t in options:
        if lowered == t.value.lower() or text == INTENT_LABELS_ZH.get(t, t.value):
            return t
    return None
