"""wire 消息家族：基类 + 按角色分的四个消息类型。

`Message` 是基类，`role` 由子类以 ``ClassVar`` 固定——构造时不用（也传不进去）
写 role。四条消息在 wire 上形状相同、可互相回放：`chat` / `chat_stream` 返回的
就是一条 ``AssistantMessage``，它随即进 in-context 窗口、下一轮原样作为请求消息
发回。角色特有字段只出现在对应子类上：`tool_calls` 属于 assistant、
`tool_call_id` 属于 tool。
"""
from paperflow.core.common.constants import MessageRole
from paperflow.core.llm.domain.dto.messages.assistant import AssistantMessage
from paperflow.core.llm.domain.dto.messages.base import Message
from paperflow.core.llm.domain.dto.messages.system import SystemMessage
from paperflow.core.llm.domain.dto.messages.tool import ToolMessage
from paperflow.core.llm.domain.dto.messages.user import UserMessage

__all__ = [
    "Message",
    "SystemMessage",
    "UserMessage",
    "AssistantMessage",
    "ToolMessage",
    "message_from_role",
]

_ROLE_TYPES: dict[MessageRole, type] = {
    MessageRole.system: SystemMessage,
    MessageRole.user: UserMessage,
    MessageRole.assistant: AssistantMessage,
    MessageRole.tool: ToolMessage,
}


def message_from_role(role, *, content="", tool_calls=None, tool_call_id=None):
    """按角色重建一条消息——持久化层的 role 是枚举，回放时在这里还原成具体类型。

    从 SQL 回放 in-context 窗口时只有角色字符串可用（messages 表按行存 role），
    这里是把「角色」还原成「类型」的唯一漏斗。不传本角色没有的字段（tool_calls
    只给 assistant、tool_call_id 只给 tool），避免造出「形状对了但语义不成立」的消息。

    Args:
        role: str | MessageRole，消息角色。
        content: 消息正文（str 或 content parts 列表）。
        tool_calls: 工具调用列表，仅 assistant 角色有意义。
        tool_call_id: 关联的调用 ID，仅 tool 角色有意义。

    Returns:
        对应角色的消息实例。

    Raises:
        ValueError: 角色名不在四个已知角色内。
    """
    try:
        role = MessageRole(role)
    except ValueError as e:
        raise ValueError(f"未知消息角色：{role!r}") from e
    cls = _ROLE_TYPES[role]
    if cls is AssistantMessage:
        return AssistantMessage(content=content, tool_calls=tool_calls)
    if cls is ToolMessage:
        return ToolMessage(content=content, tool_call_id=tool_call_id)
    return cls(content=content)
