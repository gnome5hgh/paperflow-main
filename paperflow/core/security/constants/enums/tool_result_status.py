"""工具执行的结果状态词表（审计 `result_status` 字段）。

与 `policy_decision` 是两条正交的轴：那条说「策略怎么判的」，这条说「最后落地成
什么」——策略放行了但工具自己抛错，就是 `AUTO_ALLOWED` + `ERROR`。
"""

from enum import StrEnum

__all__ = ["ToolResultStatus"]


class ToolResultStatus(StrEnum):
    """一次工具调用在结果侧的结论。"""

    #: 执行完成，无异常
    SUCCESS = "success"
    #: 被策略阻断。**当前不可达**：策略层只升级确认、不拒绝（同 ToolDecision.POLICY_DENIED）
    POLICY_BLOCKED = "policy_blocked"
    #: 被安全机制拦截（内容防护 / 路径黑名单 / 命令熔断）
    SECURITY_BLOCKED = "security_blocked"
    #: 用户拒绝或确认未落地
    USER_DENIED = "user_denied"
    #: 执行期普通异常
    ERROR = "error"
