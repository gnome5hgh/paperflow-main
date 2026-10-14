"""模型回复载体：一次 LLM 调用返回的 assistant 消息 + 回复才有的截断标记。

字段形状与请求侧的 ``Message`` 完全一致（回复本来就是一条 assistant 消息），所以
直接继承它——消费方（ReAct 循环、结构化抽取、多模态看图）按 ``.content`` /
``.tool_calls`` 读取即可，不必先拆一层；同时请求侧的信封不必再背一个只对回复
有意义的字段。
"""
from dataclasses import dataclass

from paperflow.core.llm.domain.dto.message import Message


@dataclass
class LLMResponse(Message):
    """模型对一次 ``chat`` / ``chat_stream`` 调用的回复。

    Attributes:
        truncated: bool，响应因输出长度上限被截断（``finish_reason == "length"``）。
            Agent 据此走截断续写，而不是把半截内容当最终回答交付。
    """

    #: 响应被输出长度上限截断——只对回复有意义，故不挂在请求侧的 Message 上
    truncated: bool = False
