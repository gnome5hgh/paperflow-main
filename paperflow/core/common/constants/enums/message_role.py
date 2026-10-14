"""对话消息的角色。

被两条链共同消费：LLM 的 wire 消息家族（各角色子类用它固定自己的 role）与记忆层
的持久化消息（messages 表的 role 列存的就是它的值）。没有单一子包归属，故住
`core/common`。
"""

from enum import StrEnum

__all__ = ["MessageRole"]


class MessageRole(StrEnum):
    """对话消息的角色枚举（与 OpenAI wire 的角色名一致）。

    用于标识每条消息的来源角色：
        - system: 系统提示词
        - user: 用户输入
        - assistant: 模型输出
        - tool: 工具调用结果（与 tool_call_id 关联）

    继承 StrEnum 是刻意的：角色名是 wire 契约的一部分，与 API 对齐优先于封闭比较，
    因此 ``role == "system"`` 这类与字符串字面量的比较仍然成立。
    """

    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"
