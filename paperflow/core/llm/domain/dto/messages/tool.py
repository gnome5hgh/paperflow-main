"""tool 消息：工具执行结果。"""
from dataclasses import dataclass
from typing import ClassVar

from paperflow.core.common.constants import MessageRole
from paperflow.core.llm.domain.dto.messages.base import Message

__all__ = ["ToolMessage"]


@dataclass
class ToolMessage(Message):
    """tool 消息：一次工具调用的结果，靠 tool_call_id 挂回发起它的 tool_call。

    结果内容是**外部数据**（检索命中、文件内容、网页正文），可能夹带伪装成指令的
    文本——内容防护据此打来源标记，模型侧则应只当数据读。
    """

    role: ClassVar[MessageRole] = MessageRole.tool

    #: 关联的工具调用 ID，把结果挂回对应的 tool_call。缺了它（或配不上对）时
    #: OpenAI 会以 400 拒绝整个请求，故历史自愈会为孤儿 tool_call 补一条合成消息。
    tool_call_id: str | None = None
