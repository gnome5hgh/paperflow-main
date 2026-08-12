"""子 agent 运行模式枚举——spawn 派发的跨层契约（单一真相源）。

父 agent spawn 子 agent 时经 mode 参数传入，spawn 注入 `当前模式：{mode}` 到子
agent 的 system prompt；子 agent 的 SKILL 据此判别走哪个流程。值即 SKILL 里使用的
字符串字面量。只覆盖有确定性 ground truth 的父子对——qa-agent 自选不传（枚举不含
其值，不传 mode 的 spawn 行为不受影响）。
"""

from enum import Enum

__all__ = ["SubAgentMode", "SUB_AGENT_MODES"]


class SubAgentMode(str, Enum):
    """子 agent 运行模式。值 = SKILL 判别用的字符串，str 枚举与字面量等价。"""

    #: writer：笔记流程（generate_note 派发）
    NOTE = "note"
    #: writer：大纲流程（write_outline 派发）
    OUTLINE = "outline"
    #: reviewer：笔记审稿（writer 笔记流程 spawn）
    NOTE_REVIEW = "note_review"
    #: reviewer：大纲审稿（writer 大纲流程 spawn）
    OUTLINE_REVIEW = "outline_review"
    #: reviewer：下载门禁（searcher spawn）
    DOWNLOAD_REVIEW = "download_review"


#: 合法 mode 值集合——spawn 运行时校验兜底（schema enum 约束 LLM 生成层，
#: 此集合兜住任何漏网之鱼，防拼写错静默错流）。
SUB_AGENT_MODES = frozenset(m.value for m in SubAgentMode)
