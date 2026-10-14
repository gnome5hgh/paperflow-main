"""wire 消息基类。

它等价于 OpenAI Chat Completion 的一条 message，用 dataclass 而非 SDK 内部类型
表达，便于序列化与上下文管理（增删改统一走它，不依赖 SDK 类型）。

**角色决定类型**：四个子类各固定一个 role，构造时不用（也传不进去）写 role——
角色写错这件事从类型层面消失。角色特有字段只出现在对应子类上，请求侧消息不会
背上只对回复有意义的字段。
"""
from dataclasses import dataclass
from typing import ClassVar

from paperflow.core.common.constants import MessageRole

__all__ = ["Message"]


@dataclass
class Message:
    """一条 wire 消息，等价于 OpenAI Chat Completion 的一条 message。

    **进出两个方向共用这一族类型**：`chat` / `chat_stream` 返回的就是一条
    ``AssistantMessage``，而它随即被 append 进 in-context 窗口、下一轮原样作为
    请求消息发回去——回复与请求在 wire 上本来就是同一个形状，所以不为「回复」
    另设类型。

    基类本身不含 role（它没有具体角色），不可直接构造；role 由每个子类以
    ``ClassVar`` 固定，读 ``m.role`` 时拿到的是该子类的角色枚举。

    Attributes:
        content: str | list[dict]，消息正文。视觉调用时为 OpenAI content parts
            列表（text / image_url），ReAct 对话恒为 str；纯工具调用的 assistant
            消息可为空串。
    """

    #: 消息角色：由子类以 ClassVar 固定，基类不持有具体值。取到的是
    #: ``MessageRole`` 枚举——它继承自 str，故 ``m.role == "system"`` 这类与
    #: 字符串字面量的比较依然成立。
    role: ClassVar[MessageRole]

    #: 消息正文，纯工具调用轮可为空串；
    #: 视觉调用时为 OpenAI content parts 列表（text / image_url）
    content: str | list[dict] = ""
