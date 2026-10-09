"""spawn 派发的枚举——派发结局。

结果状态是 SubAgentResult 与派发账本共用的词汇，会进 LLM 可见的结果 JSON 与
收尾核对账本，改值等于改提示词契约。
"""

from enum import StrEnum

__all__ = ["SubAgentStatus"]


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
