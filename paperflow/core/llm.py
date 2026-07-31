# paperflow/core/llm.py
"""
LLM 客户端 —— OpenAI-compatible API 的异步封装。

设计要点：

1. **异步接口 = 同步 SDK + asyncio.to_thread**
   Layer 0 使用 ``asyncio.to_thread`` 将 OpenAI SDK 的同步阻塞调用
   包装为 async 协程。这保证了 ``Agent.run`` 的整体异步性，
   后续 ParallelSpawnTool（Layer 4）可直接用 ``asyncio.gather`` 并发调度。

2. **Message 是 LLM ↔ Agent 的唯一数据货币**
   ReAct 循环中消息的增删改统一走 ``Message`` dataclass，
   不依赖 OpenAI SDK 的内部类型，方便序列化和上下文管理。

3. **tools 参数直接传 OpenAI 格式的 JSON Schema**
   不做中间抽象层 —— ``tool_to_openai_schema()`` 直接将 ``Tool`` 对象
   转为 function calling 的 JSON Schema dict，LLM 不感知工具实现细节。

4. **非流式调用**
   ReAct 循环需要完整解析 tool_calls 才能决定下一步，
   流式响应增加复杂度且 LLM 推理延迟本身远大于 streaming 节省的时间。
"""

import asyncio
from dataclasses import dataclass

from openai import OpenAI

from paperflow.config import LLMConfig


@dataclass
class Message:
    """
    ReAct 循环中的一条消息，等价于 OpenAI Chat Completion 的一条 message。

    不同角色的 message 使用不同的字段组合：

    - system / user / assistant（无 tool_calls）：只需 role + content
    - assistant（有 tool_calls）：role + tool_calls（content 可为空）
    - tool（工具执行结果）：role + content + tool_call_id
    """

    #: 消息角色："system" | "user" | "assistant" | "tool"
    role: str

    #: 消息正文，tool_calls 消息此项可为空字符串
    content: str

    #: LLM 返回的工具调用列表，仅 assistant 消息有值
    #: 每个元素为: {"id": str, "type": "function", "function": {"name": ..., "arguments": ...}}
    tool_calls: list[dict] | None = None

    #: 关联的工具调用 ID，仅 tool 角色消息有值，用于将 tool result 关联到对应的 tool_call
    tool_call_id: str | None = None


class LLMClient:
    """
    OpenAI-compatible API 的异步客户端封装。

    使用 OpenAI Python SDK 进行底层 HTTP 通信，
    通过 asyncio.to_thread 将同步调用转为 async，
    保持与 Agent.run 的 async/await 一致。

    使用方式::

        config = LLMConfig(api_key="sk-xxx")
        client = LLMClient(config)
        response = await client.chat(messages, tools=schema_list)
    """

    def __init__(self, config: LLMConfig):
        """
        :param config: LLMConfig 实例，包含 base_url / api_key / model 等参数
        """
        #: OpenAI SDK 客户端实例（底层 httpx 连接池，线程安全）
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key)

        #: 模型名称，每次 chat 调用传给 API
        self.model = config.model

        #: 单次请求最大输出 token
        self.max_tokens = config.max_tokens

        #: 采样温度，0.0 = 确定性输出
        self.temperature = config.temperature

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
    ) -> Message:
        """
        单次非流式 LLM 调用，返回 assistant message（可能包含 tool_calls）。

        :param messages: 对话历史，第一条通常为 system prompt
        :param tools: 可用的 Tool 定义列表（JSON Schema 格式），None 表示不传 tools 参数
        :param tool_choice: "auto" 由 LLM 决定是否调用工具，"none" 禁止，"required" 强制
        :returns: 封装后的 assistant Message
        :raises: SDK 异常直接向上抛，由 Agent 自行决定是否 recover
        """
        # 构建 API 请求参数
        kwargs = dict(
            model=self.model,
            messages=[_message_to_openai(m) for m in messages],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )

        # tools 和 tool_choice 仅在有可用工具时传入
        # （OpenAI API 要求 tool_choice 必须与 tools 同时存在）
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        # 将同步阻塞的 SDK 调用包装为 async，释放事件循环给其他协程
        response = await asyncio.to_thread(
            self.client.chat.completions.create, **kwargs
        )
        choice = response.choices[0].message

        # 解析 tool_calls：提取 id / type / function name / arguments
        tool_calls = None
        if choice.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                        # arguments 由 LLM 返回 JSON 字符串，Agent._exec_tool 负责 json.loads
                    },
                }
                for tc in choice.tool_calls
            ]

        # content 可能是 None（OpenAI 2.x 行为：纯 tool-call 响应不含 content）
        return Message(
            role=choice.role,
            content=choice.content or "",
            tool_calls=tool_calls,
        )


def _message_to_openai(m: Message) -> dict:
    """
    将内部 Message 转为 OpenAI API 接受的 dict 格式。

    只有非 None 的字段才会出现在输出中 ——
    OpenAI API 拒绝 null tool_call_id 或空 tool_calls 字段。
    """
    msg: dict = {"role": m.role, "content": m.content}
    if m.tool_calls is not None:
        msg["tool_calls"] = m.tool_calls
    if m.tool_call_id is not None:
        msg["tool_call_id"] = m.tool_call_id
    return msg


def tool_to_openai_schema(t) -> dict:
    """
    将 Tool 实例转为 OpenAI function calling JSON Schema。

    :param t: Tool 子类实例
    :returns: {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}

    这个函数是 Tool 抽象层到 LLM API 的桥接 ——
    Tool 开发者只需定义类的 name/description/parameters 属性，
    Agent 通过此函数自动将工具描述发给 LLM，LLM 据此决定何时以及如何调用工具。
    """
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }
