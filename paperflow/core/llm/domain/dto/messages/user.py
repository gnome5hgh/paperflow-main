"""user 消息：用户输入，以及喂给模型的合成指令。"""
from dataclasses import dataclass
from typing import ClassVar

from paperflow.core.common.constants import MessageRole
from paperflow.core.llm.domain.dto.messages.base import Message

__all__ = ["UserMessage"]


@dataclass
class UserMessage(Message):
    """user 消息：本轮用户输入。

    也承载两类合成指令（截断续写提示、结构化输出的校验纠错回喂）；视觉调用用它
    装「文本 + 图」的 content parts。
    """

    role: ClassVar[MessageRole] = MessageRole.user
