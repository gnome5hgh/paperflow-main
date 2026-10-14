"""审批结果词表：一次「要不要用户点头」的流程最终落到哪个结果。

`Agent` 判定确认结果后写入 `ToolContext.approval_outcome`，经审计落进
`approval_outcome` 字段（`approval_decided` 事件与 `tool_ended` 事件的同一个键）。
取值也是 `ToolResult.summary["decision"]` 在拒绝路径上的取值。
"""

from enum import StrEnum

__all__ = ["ApprovalOutcome"]


class ApprovalOutcome(StrEnum):
    """人工确认流程的结果。"""

    #: 用户放行
    USER_CONFIRMED = "user_confirmed"
    #: 用户拒绝（有人在，明确说不）
    USER_DENIED = "user_denied"
    #: 无人值守默认拒绝（fail-safe：没有真实人工回调时一律拒）
    AUTO_DENIED = "auto_denied"
    #: 确认过程中被取消（Ctrl+C），决策未落地——审计据此结算悬空的 requested
    CANCELLED = "cancelled"
