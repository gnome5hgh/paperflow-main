"""spawn 派发的枚举——派发结局。

结果状态是 SubAgentResult 与 LLM 可见结果 JSON 共用的词汇，改值等于改提示词契约。
"""

from enum import StrEnum

__all__ = ["SubAgentStatus"]


class SubAgentStatus(StrEnum):
    """一次派发的结局——子任务的结果状态。"""

    SUCCESS = "success"
    FAILED = "failed"
    TIMEOUT = "timeout"
    #: 被拒（子任务未跑，需用户介入修正）
    DENIED = "denied"
