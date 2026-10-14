"""工具调用的最终决策词表。

两个消费点共用同一套取值：`SecurityError.decision` 字段（异常自报的决策类型）与
审计 `policy_decision`（一次调用的策略结论）。同一个词在两个地方指同一件事，
所以只在这里声明一次。
"""

from enum import StrEnum

__all__ = ["ToolDecision"]


class ToolDecision(StrEnum):
    """一次工具调用的策略结论。"""

    #: 未触发任何升级因，策略层直接放行
    AUTO_ALLOWED = "auto_allowed"
    #: 用户确认后放行
    USER_CONFIRMED = "user_confirmed"
    #: 用户拒绝（含确认超时未答——未确认即未发生，一律记为拒绝）
    USER_DENIED = "user_denied"
    #: 无人值守 fail-safe 拒绝
    AUTO_DENIED = "auto_denied"
    #: 安全拦截（内容防护 / 路径黑名单 / 命令熔断）
    SECURITY_BLOCKED = "security_blocked"
    #: 策略拒绝。**当前不可达**：策略层只升级确认、不拒绝，这个值作为审计 schema 的
    #: 合法取值保留（历史数据里存在），但现在的代码路径不会产生它。
    POLICY_DENIED = "policy_denied"
    #: 尚未决定：等用户确认。不落审计的 policy_decision（那条路径记为 user_denied），
    #: 仅作为异常自身的决策标签。
    CONFIRM_REQUIRED = "confirm_required"
    #: 普通异常（非安全类）兜底
    ERROR = "error"
