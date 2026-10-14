"""流式事件的类型。

事件由 runtime 产出、被终端渲染层与 spawn 遥测消费。之前两边各写字符串字面量，
加一个事件类型就要在两个包里同时改；词表单列后，产出一侧用枚举成员、
消费一侧按它分发。
"""

from enum import StrEnum

__all__ = ["StreamEventKind"]


class StreamEventKind(StrEnum):
    """一次流式事件的类型。"""

    #: 正文增量（text 为新增片段）
    CONTENT = "content"
    #: 工具调用开始（携工具名与活动行摘要）
    TOOL_START = "tool_start"
    #: 工具调用结束（携耗时、写类 diffstat 与完成行文本）
    TOOL_END = "tool_end"
