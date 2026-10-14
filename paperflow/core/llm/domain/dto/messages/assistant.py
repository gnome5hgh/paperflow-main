"""assistant 消息：模型输出。"""
from dataclasses import dataclass
from typing import ClassVar

from paperflow.core.common.constants import MessageRole
from paperflow.core.llm.domain.dto.messages.base import Message
from paperflow.core.llm.domain.dto.response_meta import ResponseMeta

__all__ = ["AssistantMessage"]


@dataclass
class AssistantMessage(Message):
    """assistant 消息：模型输出。既可携带工具调用，也是回复的载体。

    本类型同时是「回复」与「请求历史」：chat / chat_stream 返回的就是它，下一轮
    原样回放为请求消息。信封（meta）与截断标记只对回复有意义，请求侧取默认值即可。
    """

    role: ClassVar[MessageRole] = MessageRole.assistant

    #: 模型请求调用的工具列表，无工具调用时为 None。每个元素为
    #: {"id": str, "type": "function", "function": {"name": ..., "arguments": ...}}
    #: ——arguments 是 JSON 字符串，由 Agent._exec_tool 负责 json.loads。
    tool_calls: list[dict] | None = None

    #: 响应信封（finish_reason / token 用量 / 模型快照等），从端点响应派生。
    #: **不上 wire**：出站只取 content 与 tool_calls；回放历史时它随对象留在进程内。
    meta: ResponseMeta | None = None

    @property
    def truncated(self) -> bool:
        """响应因输出长度上限被截断（``finish_reason == "length"``）。

        Agent 据此续写而非把半截内容当最终回答——否则长笔记草稿会被静默截断成残缺
        内容交付。由信封派生而非另存字段：两个来源各存一份迟早互相漂移。没有信封
        （如测试里的假回复）即视为未截断。
        """
        return self.meta is not None and self.meta.finish_reason == "length"
