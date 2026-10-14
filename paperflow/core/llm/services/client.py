# paperflow/core/llm/services/client.py
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
   反馈而设：stream=True + on_delta 回调，返回与 chat() 同形状的 `Message`。
"""

import asyncio
import re
import time
from urllib.parse import urlsplit
from datetime import datetime

import httpx

from paperflow.core.security.services import endpoint_guard
from openai import OpenAI

from paperflow.config import LLMConfig
from paperflow.core.llm.domain.dto.messages import AssistantMessage, Message, ToolMessage
from paperflow.core.llm.domain.dto.response_meta import ResponseMeta
from paperflow.core.llm.domain.dto.usage import Usage


class LLMClient:
    """OpenAI-compatible API 的异步客户端封装。

    使用 OpenAI Python SDK 进行底层 HTTP 通信，
    通过 asyncio.to_thread 将同步调用转为 async，
    保持与 Agent.run 的 async/await 一致。

    使用方式::

        config = LLMConfig(api_key="sk-xxx")
        client = LLMClient(config)
        response = await client.chat(messages, tools=schema_list)

    Attributes:
        client: OpenAI，SDK 客户端实例（底层 httpx 连接池，线程安全）
        model: str，模型名
        max_tokens: int，单次请求最大输出 token
        temperature: float，采样温度（0.0 = 确定性）
        context_window: int | None，模型上下文窗口（压缩时推导预算）
    """

    def __init__(self, config: LLMConfig):
        """
        Args:
            config: LLMConfig 实例，包含 base_url / api_key / model 等参数

        Raises:
            RuntimeError: api_key 为空时 fail-fast——留空会触发 SDK 晦涩报错，这里提前抛出带配置指引的可行动错误（密钥不从代码硬编码默认值）

        """
        #: key 守卫:api_key 不再有代码默认值,留空时 OpenAI(api_key="") 抛晦涩的
        #: SDK 错误——此处提前 fail-fast,给出可行动的配置指引。
        if not config.api_key:
            raise RuntimeError(
                "LLM API key 未配置：请设置环境变量 PAPERFLOW_LLM_API_KEY"
                "（或在 .env 文件设置同名变量），或在 config.yaml 的 llm.api_key 提供"
            )
        #: OpenAI SDK 客户端实例（底层 httpx 连接池，线程安全）。
        #: 显式超时：SDK 默认 read 600s，一次 HTTP 挂死会让 UI 长时间空转。
        #: read 超时同时约束流式相邻 chunk 的间隔——首包超时
        #: 天然覆盖，无需另写逻辑；write/pool 对齐 read/connect 的量级。
        # 自建 http_client 而不是让 SDK 建：为了挂上出网绑定钩子——每次请求发出前
        # 校验目标 host 与配置一致（配置说 A 就必须去 A），并禁跨 host 重定向。
        # 超时随之下移到客户端上（SDK 不再替我们建）。
        _http = httpx.Client(
            timeout=httpx.Timeout(
                connect=config.timeout_connect,
                read=config.timeout_read,
                write=config.timeout_read,
                pool=config.timeout_connect,
            ),
            event_hooks={"request": [endpoint_guard(config.base_url)]},
            follow_redirects=False,
        )
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key,
            http_client=_http,
            max_retries=config.max_retries,
        )

        #: 模型名称，每次 chat 调用传给 API
        self.model = config.model

        #: 端点网络位置（base_url 的 host:port）——审计标注模型来自哪个端点用
        self.provider = urlsplit(config.base_url).netloc

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
    ) -> AssistantMessage:
        """
        单次非流式 LLM 调用，返回 assistant 消息（可能包含 tool_calls）。
        流式变体见 chat_stream()。

        Args:
            messages: 对话历史，第一条通常为 system prompt
            tools: 可用的 Tool 定义列表（JSON Schema 格式），None 表示不传 tools 参数
            tool_choice: "auto" 由 LLM 决定是否调用工具，"none" 禁止，"required" 强制
            json_mode: True 时传 response_format="json_object" 强制 JSON 输出
            temperature: 单次调用温度覆盖，None 表示用 config 默认值
            extra_body: 附加请求体参数（如 DeepSeek 的 enable_thinking），端点为不支持时自动降级重试一次
            telemetry_callback: 调用结束后同步回调元数据 dict （model/response_model/provider/input_tokens/output_tokens/duration_ms/started_at/finish_reason），供审计 replay 使用；不含消息正文。None 时零开销跳过

        Returns:
            封装后的 assistant 消息（``AssistantMessage``，含 tool_calls 与响应信封 ``meta``）

        Raises:
            SDK 异常直接向上抛，由 Agent 自行决定是否 recover

        """
        # 计时起点：duration_ms 覆盖从入参到返回的完整调用耗时（含降级重试），
        # started_at 记调用起点墙钟，供审计 replay 推算 ended_at（mtime 不可写审计）
        _started = time.monotonic()
        _started_wall = datetime.now().isoformat()

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

        # 将同步阻塞的 SDK 调用包装为 async，释放事件循环给其他协程，使其可以继续调度其他协程。当线程中的函数执行完毕后，通过 await 获取返回值。
        # self.client.chat.completions.create(**kwargs) 是一个 同步阻塞函数，整个 asyncio 事件循环会被卡住，所有其他协程（如并发请求、WebSocket、定时任务等）全部暂停，直到该 HTTP 请求返回。
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

        # response：上一行调用 openai SDK 的 chat.completions.create(...) 返回的原始响应对象（LLM 给你的完整答复）。它的结构大致是：
        #   response = {
        #       "choices": [            # 候选完成列表（通常只有 1 个）
        #           {
        #               "message": {     # 助手消息 = 模型实际输出的内容
        #                   "role": "assistant",
        #                   "content": "……回答文本……",   # 可能是 None（纯工具调用时）
        #                   "tool_calls": [ ... ],         # 模型请求调用工具时的列表
        #               },
        #               "finish_reason": "stop",           # 结束原因（stop/length/...）
        #           }
        #       ],
        #       "usage": {...},         # token 用量
        #   }
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
        # meta 是响应信封：finish_reason 派生 truncated，usage/模型快照供审计与排查
        meta = ResponseMeta(
            id=getattr(response, "id", None),
            model=getattr(response, "model", None),
            created=getattr(response, "created", None),
            finish_reason=response.choices[0].finish_reason,
            usage=_extract_usage(getattr(response, "usage", None)),
            refusal=getattr(choice, "refusal", None),
            reasoning_content=getattr(choice, "reasoning_content", None),
            system_fingerprint=getattr(response, "system_fingerprint", None),
        )
        msg = AssistantMessage(
            content=choice.content or "",
            tool_calls=tool_calls,
            meta=meta,
        )

        # telemetry：只回传元数据（usage/duration_ms/started_at/finish_reason），
        # 绝不包含消息正文。回调不存在时是零开销 no-op
        if telemetry_callback is not None:
            telemetry_callback(self._telemetry_fields(meta, _started, _started_wall))
        return msg

    async def chat_stream(self, messages, tools=None, tool_choice="auto",
                          on_delta=None, telemetry_callback=None) -> AssistantMessage:
        """流式版 chat()：stream=True + 边收边回调 on_delta，返回完整回复。

        仅 Agent（ReAct）消费；StructuredOutput 等要完整 JSON 的调用方继续用 chat()。
        Args:
            on_delta: 每段 content 片段同步回调（跑在 to_thread 流线程内—— 非主事件循环线程，回调只能做追加/打印，别碰事件循环）
            telemetry_callback: 与 chat() 同语义的元数据回调，token 数来自 最后一个带 usage 的 chunk；端点不支持流式 usage 时 tokens 记 None。流式 token 归因依赖 stream_options={"include_usage": True}（OpenAI 兼容 端点默认不返回流式 usage）；老端点不支持该参数时自动降级重试一次

        """
        # 计时起点：duration_ms 覆盖整个流式接收过程；started_at 记调用起点墙钟
        _started = time.monotonic()
        _started_wall = datetime.now().isoformat()

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

        def _do_stream() -> AssistantMessage:
            """在同一个线程内创建流并迭代收齐结果（Stream 是同步迭代器，交回事件循环会阻塞 loop）。

            Returns:
                累加完成后的 assistant 消息（``AssistantMessage``，含响应信封）。
            """
            # create 与 iterate 必须同线程：Stream 是同步迭代器，逐 chunk 阻塞在
            # httpx 读取上；不能把 Stream 交回事件循环再迭代（否则阻塞 loop，
            # 杀死并发子 agent 的并行调用）。
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
            content, tool_calls, meta = _accumulate_stream_chunks(stream, on_delta)
            if telemetry_callback is not None:
                # 流式 usage 仅部分端点提供（多在收尾 chunk 上）；缺失时元数据
                # 留空，不落 content——与 chat() 一致地只回传元数据
                telemetry_callback(self._telemetry_fields(meta, _started, _started_wall))
            return AssistantMessage(content=content, tool_calls=tool_calls, meta=meta)

        return await asyncio.to_thread(_do_stream)

    def _telemetry_fields(self, meta: ResponseMeta, started: float,
                          started_wall: str) -> dict:
        """把响应信封 + 调用点信息拼成审计用的元数据 dict。

        信封（模型快照 / finish_reason / token 用量）由响应派生，是唯一抽取点；
        provider / duration_ms / started_at 不是响应内容而是调用点信息，在这里补上。
        **不含消息正文**。usage 缺失（端点未报）时 token 两项留 None。

        Args:
            meta: 本次调用的响应信封
            started: time.monotonic() 起点，用于算 duration_ms
            started_wall: 调用起点的墙钟时间（ISO 字符串），供审计 replay 推算结束时刻

        Returns:
            供 telemetry_callback 消费的元数据 dict。
        """
        usage = meta.usage
        return {
            "model": self.model,
            "response_model": meta.model,
            "provider": self.provider,
            "input_tokens": usage.prompt_tokens if usage else None,
            "output_tokens": usage.completion_tokens if usage else None,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "started_at": started_wall,
            "finish_reason": meta.finish_reason,
        }


def _accumulate_stream_chunks(chunks, on_delta):
    """把 OpenAI 流式 chunks 累加为 (content, tool_calls, meta)。

    on_delta 每收到一段 content 片段即同步回调（跑在流线程内，须线程安全）。

    正文与 tool_calls 从 delta 分片累加；信封（``ResponseMeta``）在同一个循环里收齐：
    finish_reason / usage / model / id / created / system_fingerprint 都取**最后一个带值
    的 chunk**（这些字段常只出现在尾部收尾 chunk），refusal 与 reasoning_content 是
    delta 上的流式分片，按拼接处理。

    不累加角色：一次补全恒为 assistant 输出，调用方直接构造 ``AssistantMessage``，
    无需从 chunk 里读它。

    Args:
        chunks: 可迭代的 OpenAI 流式 chunk 序列
        on_delta: 回调 | None，每收到一段 content 片段即同步调用（跑在流线程内，须线程安全）

    Returns:
        ``(content, tool_calls, meta)``；content 为空串表示没有文本增量，tool_calls 无则 None。
    """
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    refusal_parts: list[str] = []
    tool_acc: dict[int, dict] = {}
    finish_reason = None
    usage_raw = None
    model = None
    resp_id = None
    created = None
    fingerprint = None
    for chunk in chunks:
        # 流式 usage / model 快照仅部分端点提供且常出现在收尾 chunk（该 chunk 可能
        # choices 为空），故在跳过空 choices 前收集，最后带值者胜出；getattr 兜底简化 fake chunk
        if getattr(chunk, "usage", None):
            usage_raw = chunk.usage
        if getattr(chunk, "model", None):
            model = chunk.model
        if getattr(chunk, "id", None):
            resp_id = chunk.id
        if getattr(chunk, "created", None) is not None:
            created = chunk.created
        if getattr(chunk, "system_fingerprint", None):
            fingerprint = chunk.system_fingerprint
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
        # tool-call 类型的 chunk 常无 content（None）——不回调、不累积
        if delta.content:
            content_parts.append(delta.content)
            if on_delta:
                on_delta(delta.content)
        # 思维链与拒答是 delta 上的流式分片（非标准字段，getattr 兜底）
        reasoning = getattr(delta, "reasoning_content", None)
        if reasoning:
            reasoning_parts.append(reasoning)
        refusal = getattr(delta, "refusal", None)
        if refusal:
            refusal_parts.append(refusal)
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
    meta = ResponseMeta(
        id=resp_id,
        model=model,
        created=created,
        finish_reason=finish_reason,
        usage=_extract_usage(usage_raw),
        refusal="".join(refusal_parts) or None,
        reasoning_content="".join(reasoning_parts) or None,
        system_fingerprint=fingerprint,
    )
    return "".join(content_parts), tool_calls, meta


def _extract_usage(raw) -> Usage | None:
    """把端点返回的 usage 对象转成 ``Usage``。

    端点给什么就记什么，缺的项留 None（「没报」与「报了 0」是两件事）；嵌套的
    cached / reasoning 明细同样按 getattr 兜底——简化版 fake usage 可能没有这两层。

    Args:
        raw: 端点响应（或收尾 chunk）上的 usage 对象；None 表示端点未报。

    Returns:
        Usage 实例；raw 为 None 时返回 None。
    """
    if raw is None:
        return None
    prompt_details = getattr(raw, "prompt_tokens_details", None)
    completion_details = getattr(raw, "completion_tokens_details", None)
    return Usage(
        prompt_tokens=getattr(raw, "prompt_tokens", None),
        completion_tokens=getattr(raw, "completion_tokens", None),
        total_tokens=getattr(raw, "total_tokens", None),
        cached_tokens=getattr(prompt_details, "cached_tokens", None),
        reasoning_tokens=getattr(completion_details, "reasoning_tokens", None),
    )


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
    """判断异常是否由"端点不支持某参数"引起。

    不同兼容端点（OpenAI / DeepSeek / vLLM / Ollama）对不支持参数的
    报错措辞各异，用正则模式匹配常见说法（如 response_format、
    enable_thinking、unknown parameter 等），命中则允许 chat 降级重试。

    Args:
        e: Exception，SDK 抛出的异常

    Returns:
        True 表示异常由「端点不支持某参数」引起（可降级去掉该参数重试）。
    """
    text = str(e)
    return any(p.search(text) for p in _UNSUPPORTED_PARAM_PATTERNS)


def _message_to_openai(m: Message) -> dict:
    """将内部消息转为 OpenAI API 接受的 dict 格式。

    只有非 None 的字段才会出现在输出中 ——
    OpenAI API 拒绝 null tool_call_id 或空 tool_calls 字段。

    角色由消息类型决定（``m.role`` 是子类固定的枚举，取 ``.value`` 出纯字符串）；
    角色特有的字段按类型取：tool_calls 只在 assistant 上、tool_call_id 只在 tool 上。

    Args:
        m: Message，内部消息对象（四个角色子类之一）

    Returns:
        OpenAI wire 格式 dict（None 字段不出现；content 经 surrogate 清洗后随消息角色给出）。
    """
    # 出站边界清洗未配对 surrogate（PDF 提取/工具结果可能携带）——否则 openai
    # SDK UTF-8 编码消息时抛 UnicodeEncodeError: surrogates not allowed，
    # 整轮 ReAct 崩溃（见 core/security/text.py）。
    from paperflow.core.common.services.text import sanitize_surrogates
    content = m.content
    if isinstance(content, list):
        # 视觉 content parts：逐 part 清洗 text 的 surrogate，图片 part 原样透传
        content = [_sanitize_content_part(p) for p in content]
    else:
        content = sanitize_surrogates(content)
    msg: dict = {"role": m.role.value, "content": content}
    if isinstance(m, AssistantMessage) and m.tool_calls is not None:
        msg["tool_calls"] = m.tool_calls
    if isinstance(m, ToolMessage) and m.tool_call_id is not None:
        msg["tool_call_id"] = m.tool_call_id
    return msg


def _sanitize_content_part(part: dict) -> dict:
    """清洗单个 content part 里的文本 surrogate；非 text part 原样返回。

    返回新 dict（不动调用方对象），图片 part 无需清洗直接透传。

    Args:
        part: dict，一个 content part（text / image_url）

    Returns:
        清洗后的新 dict（仅 text part 被改写，图片 part 原样透传）。
    """
    from paperflow.core.common.services.text import sanitize_surrogates
    if part.get("type") == "text" and isinstance(part.get("text"), str):
        part = dict(part)
        part["text"] = sanitize_surrogates(part["text"])
    return part


def tool_to_openai_schema(t) -> dict:
    """
    将 Tool 实例转为 OpenAI function calling JSON Schema。

    Args:
        t: Tool 子类实例

    Returns:
        {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}


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
