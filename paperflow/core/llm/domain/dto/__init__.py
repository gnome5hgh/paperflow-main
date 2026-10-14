"""LLM 域的层间数据载体。

``Message`` 家族是 wire 消息契约——**进出两个方向共用它**（`chat` 返回的就是一条
assistant 消息，下一轮原样回放为请求消息）；响应信封 ``ResponseMeta`` 与它内嵌的
token 用量 ``Usage`` 挂在 assistant 上，但本身不是消息，同样是跨层搬运的数据
（客户端产出、Agent 与审计消费）。

schema → prompt 渲染与结构化抽取是**服务**（``services/structured.py``），不放这里；
组件行为参数在 `core/llm/config/`。
"""
from paperflow.core.llm.domain.dto.messages import (
    AssistantMessage,
    Message,
    SystemMessage,
    ToolMessage,
    UserMessage,
    message_from_role,
)
from paperflow.core.llm.domain.dto.response_meta import ResponseMeta
from paperflow.core.llm.domain.dto.usage import Usage

__all__ = [
    "Message",
    "SystemMessage",
    "UserMessage",
    "AssistantMessage",
    "ToolMessage",
    "ResponseMeta",
    "Usage",
    "message_from_role",
]
