"""工具安全元数据的值域枚举（单一真相源）。"""

from enum import StrEnum

__all__ = ["RiskLevel", "SideEffect"]


class RiskLevel(StrEnum):
    """工具风险档位。策略引擎按会话免问上限 confirm_above 升级超过它的调用。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SideEffect(StrEnum):
    """工具声明的副作用类型（可多选）。"""

    WRITE_FILE = "write_file"
    DELETE_FILE = "delete_file"
    NETWORK = "network"
    READ_FILE = "read_file"
