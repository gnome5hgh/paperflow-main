"""记忆模块的枚举——消息角色。"""

from enum import StrEnum

__all__ = ["MessageRole"]


class MessageRole(StrEnum):
    """对话消息的角色枚举（与 OpenAI wire 的角色名一致）。

    用于标识每条消息的来源角色：
        - system: 系统提示词
        - user: 用户输入
        - assistant: 模型输出
        - tool: 工具调用结果（与 tool_call_id 关联）
    """

    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"
