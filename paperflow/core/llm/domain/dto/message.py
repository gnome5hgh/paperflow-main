"""LLM wire 消息契约：ReAct 循环与 LLM 之间唯一的数据货币。

它等价于 OpenAI Chat Completion 的一条 message，用 dataclass 而非 SDK 内部类型
表达，便于序列化与上下文管理（增删改统一走它，不依赖 SDK 类型）。
"""
from dataclasses import dataclass


@dataclass
class Message:
    """ReAct 循环中的一条消息，等价于 OpenAI Chat Completion 的一条 message。

    不同角色的 message 使用不同的字段组合：

    - system / user / assistant（无 tool_calls）：只需 role + content
    - assistant（有 tool_calls）：role + tool_calls（content 可为空）
    - tool（工具执行结果）：role + content + tool_call_id

    content 为 str 或 OpenAI content parts 列表——视觉调用用 list 携带图
    （image_url base64 data URL），ReAct 对话恒为 str。

    Attributes:
        role: str，消息角色：system | user | assistant | tool
        content: str | list[dict]，消息正文；视觉调用时为 OpenAI content parts 列表（tool_calls 消息可为空串）
        tool_calls: list[dict] | None，LLM 返回的工具调用（仅 assistant 有值）
        tool_call_id: str | None，关联的工具调用 ID（仅 tool 消息有值）
        truncated: bool，响应因输出长度被截断（finish_reason==length），Agent 据此续写而非交付半截内容
    """

    #: 消息角色："system" | "user" | "assistant" | "tool"
    role: str

    #: 消息正文，tool_calls 消息此项可为空字符串；
    #: 视觉调用时为 OpenAI content parts 列表（text / image_url）
    content: str | list[dict]

    #: LLM 返回的工具调用列表，仅 assistant 消息有值
    #: 每个元素为: {"id": str, "type": "function", "function": {"name": ..., "arguments": ...}}
    tool_calls: list[dict] | None = None

    #: 关联的工具调用 ID，仅 tool 角色消息有值，用于将 tool result 关联到对应的 tool_call
    tool_call_id: str | None = None

    #: 响应因输出长度上限被截断(finish_reason=="length")。Agent 据此续写而非把
    #: 半截内容当最终回答——否则长笔记草稿会被静默截断成残缺内容交付。
    truncated: bool = False

