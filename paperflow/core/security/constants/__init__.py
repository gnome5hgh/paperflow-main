"""安全域的跨模块词汇。

「这次调用算什么」在异常体系与审计之间流转——异常自报决策类型、审计按同一套词
落盘，两边必须对得上，故收在这里。本包只做再导出，消费方一律从
`paperflow.core.security.constants` 取，不关心内部怎么分文件。
"""
from paperflow.core.security.constants.enums import (
    ApprovalOutcome,
    ToolDecision,
    ToolResultStatus,
)

__all__ = ["ApprovalOutcome", "ToolDecision", "ToolResultStatus"]
