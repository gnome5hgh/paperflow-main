"""工具风险档位。"""

from enum import StrEnum

__all__ = ["RiskLevel"]


class RiskLevel(StrEnum):
    """工具风险档位。策略引擎按会话免问上限 confirm_above 升级超过它的调用。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
