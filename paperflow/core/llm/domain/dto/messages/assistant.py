"""assistant 消息：模型输出。"""
from dataclasses import dataclass
from typing import ClassVar

from paperflow.core.common.constants import MessageRole
from paperflow.core.llm.domain.dto.messages.base import Message

__all__ = ["AssistantMessage"]


@dataclass
class AssistantMessage(Message):
    """assistant 消息：模型输出。既可携带工具调用，也是回复的载体。

    本类型同时是「回复」与「请求历史」：chat / chat_stream 返回的就是它，下一轮
    原样回放为请求消息。截断标记只对回复有意义，请求侧取默认值即可。
    """

    role: ClassVar[MessageRole] = MessageRole.assistant

    #: 模型请求调用的工具列表，无工具调用时为 None。每个元素为
    #: {"id": str, "type": "function", "function": {"name": ..., "arguments": ...}}
    #: ——arguments 是 JSON 字符串，由 Agent._exec_tool 负责 json.loads。
    tool_calls: list[dict] | None = None

    #: 响应因输出长度上限被截断(finish_reason=="length")。Agent 据此续写而非把
    #: 半截内容当最终回答——否则长笔记草稿会被静默截断成残缺内容交付。
    #:
    #: **这一项不上 wire**（`_message_to_openai` 不输出它），它是从响应信封派生的
    #: 本地注解，只对 assistant 回复有意义；如实为 False 时等价于「没被截断」，
    #: 故请求侧回放的历史消息带默认值即可。
    truncated: bool = False
