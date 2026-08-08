# paperflow/core/llm.py
"""
LLM 客户端 —— OpenAI-compatible API 的异步封装。

设计要点：

1. **异步接口 = 同步 SDK + asyncio.to_thread**
   用 ``asyncio.to_thread`` 将 OpenAI SDK 的同步阻塞调用包装为 async 协程,
   保证 ``Agent.run`` 的整体异步性,并行派发工具可直接用 ``asyncio.gather`` 并发调度。

2. **Message 是 LLM ↔ Agent 的唯一数据货币**
   ReAct 循环中消息的增删改统一走 ``Message`` dataclass，
   不依赖 OpenAI SDK 的内部类型，方便序列化和上下文管理。

3. **tools 参数直接传 OpenAI 格式的 JSON Schema**
   不做中间抽象层 —— ``tool_to_openai_schema()`` 直接将 ``Tool`` 对象
   转为 function calling 的 JSON Schema dict，LLM 不感知工具实现细节。

4. **双模式：非流式 `chat()` + 流式 `chat_stream()`**
   `chat()` 保持非流式——ReAct 循环需要完整解析 tool_calls 才能决定下一步，
   StructuredOutput 等 JSON 抽取也要完整响应。`chat_stream()` 为 CLI 的实时
   反馈而设：stream=True + on_delta 回调，返回与 chat() 同形状的 Message。
"""

import asyncio
import re
import time
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

    #: 响应因输出长度上限被截断(finish_reason=="length")。Agent 据此续写而非把
    #: 半截内容当最终回答——否则长笔记草稿会被静默截断成残缺内容交付。
    truncated: bool = False


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
        #: key 守卫:api_key 不再有代码默认值,留空时 OpenAI(api_key="") 抛晦涩的
        #: SDK 错误——此处提前 fail-fast,给出可行动的配置指引。
        if not config.api_key:
            raise RuntimeError(
                "LLM API key 未配置：请在 .env 文件设置 PAPERFLOW_API_KEY（参考 .env.example），"
                "或设置环境变量 PAPERFLOW_API_KEY，或在 config.yaml 的 llm.api_key 提供"
            )
        #: OpenAI SDK 客户端实例（底层 httpx 连接池，线程安全）
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key)

        #: 模型名称，每次 chat 调用传给 API
        self.model = config.model

        #: 单次请求最大输出 token
        self.max_tokens = config.max_tokens

        #: 采样温度，0.0 = 确定性输出
        self.temperature = config.temperature

        #: 模型上下文窗口大小（token 数），上下文压缩时用于自动推导压缩尺寸
        self.context_window = config.context_window

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
        json_mode: bool = False,
        temperature: float | None = None,
        extra_body: dict | None = None,
        telemetry_callback=None,
    ) -> Message:
        """
        单次非流式 LLM 调用，返回 assistant message（可能包含 tool_calls）。
        流式变体见 chat_stream()。

        :param messages: 对话历史，第一条通常为 system prompt
        :param tools: 可用的 Tool 定义列表（JSON Schema 格式），None 表示不传 tools 参数
        :param tool_choice: "auto" 由 LLM 决定是否调用工具，"none" 禁止，"required" 强制
        :param json_mode: True 时传 response_format="json_object" 强制 JSON 输出
        :param temperature: 单次调用温度覆盖，None 表示用 config 默认值
        :param extra_body: 附加请求体参数（如 DeepSeek 的 enable_thinking），
            端点为不支持时自动降级重试一次
        :param telemetry_callback: 调用结束后同步回调元数据 dict
            （model/prompt_tokens/completion_tokens/total_tokens/latency_ms/
            finish_reason），供审计 replay 使用；不含消息正文。None 时零开销跳过
        :returns: 封装后的 assistant Message
        :raises: SDK 异常直接向上抛，由 Agent 自行决定是否 recover
        """
        # 计时起点：latency_ms 覆盖从入参到返回的完整调用耗时（含降级重试），
        # 供审计 replay 评估各环节耗时
        _started = time.monotonic()

        # 构建 API 请求参数
        kwargs = dict(
            model=self.model,
            messages=[_message_to_openai(m) for m in messages],
            max_tokens=self.max_tokens,
            temperature=self.temperature if temperature is None else temperature,
        )

        # tools 和 tool_choice 仅在有可用工具时传入
        # （OpenAI API 要求 tool_choice 必须与 tools 同时存在）
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        # json_mode → response_format；extra_body → 附加参数（DeepSeek 推理开关等）
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if extra_body:
            kwargs["extra_body"] = extra_body

        # 将同步阻塞的 SDK 调用包装为 async，释放事件循环给其他协程
        try:
            response = await asyncio.to_thread(
                self.client.chat.completions.create, **kwargs
            )
        except Exception as e:
            # 端点不支持 response_format / extra_body → 降级重试一次
            if (json_mode or extra_body) and _looks_like_unsupported_param(e):
                kwargs.pop("response_format", None)
                kwargs.pop("extra_body", None)
                response = await asyncio.to_thread(
                    self.client.chat.completions.create, **kwargs
                )
            else:
                raise
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
        # truncated：finish_reason=="length" 表示输出被 max_tokens 截断，调用方须续写
        msg = Message(
            role=choice.role,
            content=choice.content or "",
            tool_calls=tool_calls,
            truncated=(response.choices[0].finish_reason == "length"),
        )

        # telemetry：只回传元数据（usage/latency/finish_reason），绝不包含消息正文。
        # usage 某些端点可能缺失（getattr 兜底 None）；回调不存在时是零开销 no-op
        if telemetry_callback is not None:
            usage = getattr(response, "usage", None) or {}
            telemetry_callback({
                "model": self.model,
                "prompt_tokens": getattr(usage, "prompt_tokens", None),
                "completion_tokens": getattr(usage, "completion_tokens", None),
                "total_tokens": getattr(usage, "total_tokens", None),
                "latency_ms": int((time.monotonic() - _started) * 1000),
                "finish_reason": response.choices[0].finish_reason,
            })
        return msg

    async def chat_stream(self, messages, tools=None, tool_choice="auto",
                          on_delta=None, telemetry_callback=None) -> Message:
        """流式版 chat()：stream=True + 边收边回调 on_delta，返回完整 Message。

        仅 Agent（ReAct）消费；StructuredOutput 等要完整 JSON 的调用方继续用 chat()。
        :param on_delta: 每段 content 片段同步回调（跑在 to_thread 流线程内——
            非主事件循环线程，回调只能做追加/打印，别碰事件循环）
        :param telemetry_callback: 与 chat() 同语义的元数据回调，token 数来自
            最后一个带 usage 的 chunk；端点不支持流式 usage 时 tokens 记 None。
            流式 token 归因依赖 stream_options={"include_usage": True}（OpenAI 兼容
            端点默认不返回流式 usage）；老端点不支持该参数时自动降级重试一次
        """
        # 计时起点：latency_ms 覆盖整个流式接收过程
        _started = time.monotonic()

        kwargs = dict(
            model=self.model,
            messages=[_message_to_openai(m) for m in messages],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            stream=True,
            # 流式 token 归因依赖 include_usage：OpenAI 兼容端点默认不返回流式 usage
            stream_options={"include_usage": True},
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        def _do_stream() -> Message:
            # create 与 iterate 必须同线程：Stream 是同步迭代器，逐 chunk 阻塞在
            # httpx 读取上；不能把 Stream 交回事件循环再迭代（否则阻塞 loop，
            # 杀死 parallel_spawn 并发）。
            # 老兼容端点不支持 stream_options → 去掉该参数降级重试一次，
            # 与 chat() 的 response_format/extra_body 降级模式一致。
            try:
                stream = self.client.chat.completions.create(**kwargs)
            except Exception as e:
                if _looks_like_unsupported_param(e):
                    kwargs.pop("stream_options", None)
                    stream = self.client.chat.completions.create(**kwargs)
                else:
                    raise
            content, tool_calls, role, finish_reason, usage = _accumulate_stream_chunks(stream, on_delta)
            if telemetry_callback is not None:
                # 流式 usage 仅部分端点提供（多在收尾 chunk 上）；缺失时元数据
                # 留空，不落 content——与 chat() 一致地只回传元数据
                telemetry_callback({
                    "model": self.model,
                    "prompt_tokens": getattr(usage, "prompt_tokens", None),
                    "completion_tokens": getattr(usage, "completion_tokens", None),
                    "total_tokens": getattr(usage, "total_tokens", None),
                    "latency_ms": int((time.monotonic() - _started) * 1000),
                    "finish_reason": finish_reason,
                })
            return Message(role=role, content=content, tool_calls=tool_calls,
                           truncated=(finish_reason == "length"))

        return await asyncio.to_thread(_do_stream)


def _accumulate_stream_chunks(chunks, on_delta):
    """把 OpenAI 流式 chunks 累加为 (content, tool_calls, role, finish_reason, usage)。

    on_delta 每收到一段 content 片段即同步回调（跑在流线程内，须线程安全）。
    tool_calls 按 delta.tool_calls[].index 分片累加，arguments 分片拼接。
    finish_reason 取最后一个带值的 chunk 的（多为尾部收尾 chunk）。
    usage 取最后一个带 usage 的 chunk（部分端点仅在收尾 chunk 附带用量），
    无则 None——调用方据此将 tokens 元数据留空。
    """
    content_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    role = "assistant"
    finish_reason = None
    usage = None
    for chunk in chunks:
        # 流式 usage 仅部分端点提供且常出现在收尾 chunk（该 chunk 可能 choices 为空），
        # 故在跳过空 choices 前收集，最后带值者胜出；getattr 兜底简化 fake chunk
        if getattr(chunk, "usage", None):
            usage = chunk.usage
        # 某些端点（如 OpenAI）尾部会发一个空 choices 的 chunk，跳过避免取下标崩溃
        if not chunk.choices:
            continue
        # finish_reason 在最后一个有 choices 的 chunk 上（内容结束后带空 delta 的收尾 chunk），
        # 用"最后非 None 覆盖"而不是"只在首 chunk 读"——保证拿到的是结束信号而非中途值。
        # getattr 兜底：简化版 fake chunk 可能不设该字段（真实 SDK Choice 恒有，默认 None）
        fr = getattr(chunk.choices[0], "finish_reason", None)
        if fr:
            finish_reason = fr
        delta = chunk.choices[0].delta
        if delta.role:
            role = delta.role
        # tool-call 类型的 chunk 常无 content（None）——不回调、不累积
        if delta.content:
            content_parts.append(delta.content)
            if on_delta:
                on_delta(delta.content)
        if delta.tool_calls:
            # tool_calls 增量按 index 分片：id/name 只在首个分片出现，
            # arguments 是跨分片拼接的 JSON 字符串片段
            for tc in delta.tool_calls:
                acc = tool_acc.setdefault(
                    tc.index, {"id": None, "type": "function", "name": None, "args": []})
                if tc.id:
                    acc["id"] = tc.id
                if tc.type:
                    acc["type"] = tc.type
                if tc.function:
                    if tc.function.name:
                        acc["name"] = tc.function.name
                    if tc.function.arguments:
                        acc["args"].append(tc.function.arguments)
    tool_calls = None
    if tool_acc:
        tool_calls = [
            {"id": tool_acc[i]["id"], "type": tool_acc[i]["type"],
             "function": {"name": tool_acc[i]["name"],
                          "arguments": "".join(tool_acc[i]["args"])}}
            for i in sorted(tool_acc)
        ]
    return "".join(content_parts), tool_calls, role, finish_reason, usage


_UNSUPPORTED_PARAM_PATTERNS = [
    re.compile(r"response_format", re.IGNORECASE),
    re.compile(r"extra_body", re.IGNORECASE),
    re.compile(r"stream_options", re.IGNORECASE),
    re.compile(r"enable_thinking", re.IGNORECASE),
    re.compile(r"unknown parameter", re.IGNORECASE),
    re.compile(r"unexpected parameter", re.IGNORECASE),
    re.compile(r"unsupported parameter", re.IGNORECASE),
]


def _looks_like_unsupported_param(e: Exception) -> bool:
    """
    判断异常是否由"端点不支持某参数"引起。

    不同兼容端点（OpenAI / DeepSeek / vLLM / Ollama）对不支持参数的
    报错措辞各异，用正则模式匹配常见说法（如 response_format、
    enable_thinking、unknown parameter 等），命中则允许 chat 降级重试。
    """
    text = str(e)
    return any(p.search(text) for p in _UNSUPPORTED_PARAM_PATTERNS)


def _message_to_openai(m: Message) -> dict:
    """
    将内部 Message 转为 OpenAI API 接受的 dict 格式。

    只有非 None 的字段才会出现在输出中 ——
    OpenAI API 拒绝 null tool_call_id 或空 tool_calls 字段。
    """
    # 出站边界清洗未配对 surrogate（PDF 提取/工具结果可能携带）——否则 openai
    # SDK UTF-8 编码消息时抛 UnicodeEncodeError: surrogates not allowed，
    # 整轮 ReAct 崩溃（见 core/security/text.py）。
    from paperflow.core.security.text import sanitize_surrogates
    content = m.content
    if isinstance(content, str):
        content = sanitize_surrogates(content)
    msg: dict = {"role": m.role, "content": content}
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
