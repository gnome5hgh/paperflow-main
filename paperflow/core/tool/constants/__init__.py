"""`ToolResult.summary` 的跨模块键名。

这几个键被工具侧写入、被终端与引擎侧读出，属于「有第二个消费方」的取值契约，
故收进本包；只服务单一消费方的常量不在这里。
"""
from paperflow.core.tool.constants.constants import (
    SUMMARY_DECISION,
    SUMMARY_TERMINAL,
    SUMMARY_VIOLATIONS,
)

__all__ = ["SUMMARY_DECISION", "SUMMARY_TERMINAL", "SUMMARY_VIOLATIONS"]
