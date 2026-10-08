"""spawn 派发的枚举——运行模式与派发结局。

值即 AGENT.md 里的字符串字面量：父 agent spawn 时经 mode 参数传入，spawn 注入
`当前模式：{mode}` 到子 agent 的 system prompt，子 agent 据此判别走哪个流程。
结果状态是 SubAgentResult 与派发账本共用的词汇，会进 LLM 可见的结果 JSON 与
收尾核对账本，改值等于改提示词契约。
"""

from enum import StrEnum

__all__ = ["SubAgentMode", "SubAgentStatus"]


class SubAgentMode(StrEnum):
    """子 agent 运行模式。值 = AGENT.md 判别用的字符串，str 枚举与字面量等价。

    只覆盖有确定性 ground truth 的父子对——qa-agent 自选不传（枚举不含其值，
    不传 mode 的 spawn 行为不受影响）。noter: 笔记生成；
    reviewer: 笔记审稿 / 下载门禁 / 研究选题产物审稿。
    """

    #: noter：笔记流程（generate_note 派发）
    NOTE = "note"
    #: reviewer：笔记审稿（noter 笔记流程 spawn）
    NOTE_REVIEW = "note_review"
    #: reviewer：下载门禁（searcher spawn）
    DOWNLOAD_REVIEW = "download_review"
    #: reviewer：研究选题产物审稿（researcher 选题发现流程 spawn）
    PLAN_REVIEW = "plan_review"


class SubAgentStatus(StrEnum):
    """一次派发的结局——子任务结果与派发账本共用。

    DEDUPED 只描述「这次派发被拦下了」，子任务根本没跑，因此它只出现在派发账本，
    不是子任务的结果状态：SubAgentResult 显式拒绝该值（见其校验器）。
    """

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    #: 被拒且需用户介入时子任务同样没跑，但不属机械重复，故与 DEDUPED 分开表述
    DENIED = "denied"
    #: 同一批工具调用内的机械重复被拦下——只记账本，不进 SubAgentResult
    DEDUPED = "deduped"
