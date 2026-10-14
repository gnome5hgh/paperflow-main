"""system 消息：喂给模型的系统提示层。"""
from dataclasses import dataclass
from typing import ClassVar

from paperflow.core.common.constants import MessageRole
from paperflow.core.llm.domain.dto.messages.base import Message

__all__ = ["SystemMessage"]


@dataclass
class SystemMessage(Message):
    """system 消息：AGENT.md 系统提示、SKILLS 清单、记忆块、意图块都走这一角色。"""

    role: ClassVar[MessageRole] = MessageRole.system
