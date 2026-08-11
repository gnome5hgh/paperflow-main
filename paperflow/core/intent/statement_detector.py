# paperflow/core/intent/statement_detector.py
"""任务性判别：判断用户输入是「下任务」还是「陈述上下文」。确定性词表，不依赖 LLM。

规则（祈使标记优先于陈述框架）：
  含祈使标记（帮我/请/麻烦/…一下）→ 任务（True）
  否则命中陈述框架（我的课题是/方向是/目前/…）→ 上下文（False）
  两者皆无 → 保守 True（走既有路由，不擅自抑制派发）

边界案例：
  "我的课题是做一个circRNA关联预测框架" → 陈述框架命中 → False（记录+询问，不派发）
  "帮我搜索circRNA论文" → 祈使标记命中 → True（正常派发）
  "circRNA关联预测" → 皆无 → True（保守，走既有路由+澄清门）
"""

#: 祈使标记：命中即视为用户在下任务（优先于陈述框架）。
#: 只列「请求形式 + 动词+一下」，不列裸动词——"我的课题是做一个X"含"做"，
#: 裸动词黑名单会把陈述误判成任务（原 bug 复现）。
IMPERATIVE_MARKERS = frozenset({
    "帮我", "请", "麻烦", "能不能", "可不可以", "帮忙", "帮一下",
    "搜索一下", "查一下", "找一下", "找找", "整理一下", "总结一下",
    "看一下", "读一下", "下载一下", "推荐一下", "介绍一下",
})

#: 陈述框架：无祈使标记时命中即视为仅在陈述上下文（如课题方向）。
STATEMENT_FRAMES = frozenset({
    "我的课题是", "我的方向是", "我的目标是", "我的计划是",
    "目前", "我现在", "我正在", "我在做",
    "我想研究", "我打算", "感兴趣", "这是我的",
})


def detect_task_requested(query: str) -> bool:
    """判定 query 是「下任务」还是「陈述上下文」。

    返回 True = 任务（可派发）；False = 纯陈述（supervisor 应记录+询问，不派发）。
    祈使标记优先于陈述框架；两者皆无时保守返回 True（走既有路由+澄清门）。
    """
    if any(m in query for m in IMPERATIVE_MARKERS):
        return True
    if any(f in query for f in STATEMENT_FRAMES):
        return False
    return True
