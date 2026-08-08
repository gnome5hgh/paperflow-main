import pytest
from unittest.mock import MagicMock, patch
from paperflow.config import LLMConfig
from paperflow.core.llm import LLMClient, Message


def make_client():
    cfg = LLMConfig(api_key="sk-test", context_window=65536)
    client = LLMClient(cfg)
    client.client = MagicMock()     # 替换真实 SDK 客户端
    return client


class TestContextWindow:
    def test_reads_context_window_from_config(self):
        client = make_client()
        assert client.context_window == 65536


class TestChatJsonMode:
    @pytest.mark.asyncio
    async def test_passes_response_format(self):
        client = make_client()
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.role = "assistant"
        fake_response.choices[0].message.content = '{"ok": 1}'
        fake_response.choices[0].message.tool_calls = None
        client.client.chat.completions.create.return_value = fake_response

        msg = await client.chat([Message(role="user", content="hi")], json_mode=True)
        kwargs = client.client.chat.completions.create.call_args.kwargs
        assert kwargs["response_format"] == {"type": "json_object"}
        assert msg.content == '{"ok": 1}'
        # fake_response 未设 finish_reason（MagicMock ≠ "length"）→ 默认不截断
        assert msg.truncated is False

    @pytest.mark.asyncio
    async def test_passes_extra_body_and_temperature(self):
        client = make_client()
        fake_response = MagicMock()
        fake_response.choices = [MagicMock()]
        fake_response.choices[0].message.role = "assistant"
        fake_response.choices[0].message.content = "x"
        fake_response.choices[0].message.tool_calls = None
        client.client.chat.completions.create.return_value = fake_response

        await client.chat(
            [Message(role="user", content="hi")],
            json_mode=True, temperature=0.0,
            extra_body={"enable_thinking": False},
        )
        kwargs = client.client.chat.completions.create.call_args.kwargs
        assert kwargs["temperature"] == 0.0
        assert kwargs["extra_body"] == {"enable_thinking": False}

    @pytest.mark.asyncio
    async def test_degrades_when_endpoint_unsupported(self):
        client = make_client()
        from openai import BadRequestError
        client.client.chat.completions.create.side_effect = [
            BadRequestError(
                "response_format not supported",
                response=MagicMock(status_code=400, headers={}, request=MagicMock()),
                body={"error": {"message": "response_format not supported"}},
            ),
            MagicMock(choices=[MagicMock(
                message=MagicMock(role="assistant", content="plain", tool_calls=None))]),
        ]
        msg = await client.chat([Message(role="user", content="hi")], json_mode=True)
        assert msg.content == "plain"
        # 第二次调用不带 response_format
        calls = client.client.chat.completions.create.call_args_list
        assert "response_format" not in calls[1].kwargs

    @pytest.mark.asyncio
    async def test_degrades_when_thinking_unsupported(self):
        client = make_client()
        client.client.chat.completions.create.side_effect = [
            Exception("unknown parameter: enable_thinking"),
            MagicMock(choices=[MagicMock(
                message=MagicMock(role="assistant", content="ok", tool_calls=None))]),
        ]
        msg = await client.chat(
            [Message(role="user", content="hi")], extra_body={"enable_thinking": False}
        )
        assert msg.content == "ok"

    @pytest.mark.asyncio
    async def test_chat_marks_truncated_on_length(self):
        client = make_client()
        resp = MagicMock()
        resp.choices = [MagicMock()]
        resp.choices[0].message.role = "assistant"
        resp.choices[0].message.content = "半截"
        resp.choices[0].message.tool_calls = None
        resp.choices[0].finish_reason = "length"
        client.client.chat.completions.create.return_value = resp
        msg = await client.chat([Message(role="user", content="q")])
        assert msg.truncated is True


from types import SimpleNamespace


def _chunk(delta, choices=None):
    """构造 fake 流式 chunk：choices[0].delta，或空 choices（模拟端点尾部空 chunk）。"""
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)] if choices is None else choices)


class TestAccumulateStreamChunks:
    def test_accumulates_content_and_calls_on_delta_in_order(self):
        from paperflow.core.llm import _accumulate_stream_chunks
        deltas = []
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content="你好", tool_calls=None)),
            _chunk(SimpleNamespace(role=None, content="世界", tool_calls=None)),
        ]
        content, tool_calls, role, finish_reason, usage = _accumulate_stream_chunks(chunks, deltas.append)
        assert content == "你好世界"
        assert role == "assistant"
        assert tool_calls is None
        assert deltas == ["你好", "世界"]
        # finish_reason 无则 None
        assert finish_reason is None

    def test_accumulates_tool_calls_by_index(self):
        from paperflow.core.llm import _accumulate_stream_chunks
        tc1_first = SimpleNamespace(index=0, id="call_1", type="function",
                                    function=SimpleNamespace(name="search_paper", arguments='{"qu'))
        tc1_second = SimpleNamespace(index=0, id=None, type=None,
                                     function=SimpleNamespace(name=None, arguments='ery": "x"}'))
        tc2 = SimpleNamespace(index=1, id="call_2", type="function",
                              function=SimpleNamespace(name="read_file", arguments='{"path": "a"}'))
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content=None, tool_calls=[tc1_first])),
            _chunk(SimpleNamespace(role=None, content=None, tool_calls=[tc1_second, tc2])),
        ]
        content, tool_calls, role, finish_reason, usage = _accumulate_stream_chunks(chunks, None)
        assert content == ""
        assert tool_calls == [
            {"id": "call_1", "type": "function",
             "function": {"name": "search_paper", "arguments": '{"query": "x"}'}},
            {"id": "call_2", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
        ]
        # finish_reason 无则 None
        assert finish_reason is None

    def test_skips_empty_choices_and_none_content(self):
        from paperflow.core.llm import _accumulate_stream_chunks
        deltas = []
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content="a", tool_calls=None)),
            _chunk(None, choices=[]),          # 空 choices chunk
            _chunk(SimpleNamespace(role=None, content=None, tool_calls=None)),  # None content
        ]
        content, _, _, finish_reason, _ = _accumulate_stream_chunks(chunks, deltas.append)
        assert content == "a"
        assert deltas == ["a"]                 # None content 不回调
        # finish_reason 无则 None
        assert finish_reason is None

    def test_captures_finish_reason_length(self):
        from paperflow.core.llm import _accumulate_stream_chunks

        def chunk(content=None, finish_reason=None):
            delta = SimpleNamespace(role="assistant", content=content, tool_calls=None)
            return SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason=finish_reason)])

        chunks = [chunk("部分"), chunk("内容", finish_reason="length")]
        content, tool_calls, role, finish_reason, usage = _accumulate_stream_chunks(chunks, None)
        assert content == "部分内容"
        assert finish_reason == "length"


class TestChatStream:
    @pytest.mark.asyncio
    async def test_passes_stream_true_and_accumulates(self):
        client = make_client()
        deltas = []
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content="hi", tool_calls=None)),
            _chunk(SimpleNamespace(role=None, content="!", tool_calls=None)),
            # 尾部收尾 chunk 带 finish_reason="length"（max_tokens 截断信号）
            _chunk(SimpleNamespace(role=None, content=None, tool_calls=None),
                   choices=[SimpleNamespace(
                       delta=SimpleNamespace(role=None, content=None, tool_calls=None),
                       finish_reason="length")]),
        ]
        client.client.chat.completions.create.return_value = iter(chunks)
        msg = await client.chat_stream(
            [Message(role="user", content="x")], tools=[{}], on_delta=deltas.append)
        kwargs = client.client.chat.completions.create.call_args.kwargs
        assert kwargs["stream"] is True
        assert kwargs["tools"] == [{}]
        assert msg.content == "hi!"
        assert msg.tool_calls is None
        assert deltas == ["hi", "!"]
        # 尾 chunk finish_reason="length" → 标记截断
        assert msg.truncated is True

    @pytest.mark.asyncio
    async def test_returns_tool_calls_message(self):
        client = make_client()
        tc = SimpleNamespace(index=0, id="c1", type="function",
                             function=SimpleNamespace(name="echo", arguments='{"m": "x"}'))
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content=None, tool_calls=[tc])),
        ]
        client.client.chat.completions.create.return_value = iter(chunks)
        msg = await client.chat_stream([Message(role="user", content="x")])
        assert msg.content == ""
        assert msg.tool_calls == [{
            "id": "c1", "type": "function",
            "function": {"name": "echo", "arguments": '{"m": "x"}'},
        }]

    @pytest.mark.asyncio
    async def test_passes_stream_options_include_usage(self):
        # 流式 token 归因依赖 include_usage：OpenAI 兼容端点默认不返回流式 usage
        client = make_client()
        chunks = [_chunk(SimpleNamespace(role="assistant", content="hi", tool_calls=None))]
        client.client.chat.completions.create.return_value = iter(chunks)
        await client.chat_stream([Message(role="user", content="x")])
        kwargs = client.client.chat.completions.create.call_args.kwargs
        assert kwargs["stream_options"] == {"include_usage": True}

    @pytest.mark.asyncio
    async def test_usage_tail_chunk_reaches_telemetry(self):
        # 尾部「空 choices + usage」chunk：_accumulate_stream_chunks 在跳过空 choices
        # 前先收集 usage，最后带值者胜出 → 回调应拿到 token 数
        client = make_client()
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content="hi", tool_calls=None)),
            _chunk(SimpleNamespace(role=None, content="!", tool_calls=None)),
            _chunk(SimpleNamespace(role=None, content=None, tool_calls=None),
                   choices=[SimpleNamespace(
                       delta=SimpleNamespace(role=None, content=None, tool_calls=None),
                       finish_reason="stop")]),
            SimpleNamespace(choices=[], usage=SimpleNamespace(
                prompt_tokens=10, completion_tokens=5, total_tokens=15)),
        ]
        client.client.chat.completions.create.return_value = iter(chunks)
        captured = {}
        msg = await client.chat_stream(
            [Message(role="user", content="x")],
            telemetry_callback=lambda d: captured.update(d))
        assert msg.content == "hi!"
        assert captured["prompt_tokens"] == 10
        assert captured["completion_tokens"] == 5
        assert captured["total_tokens"] == 15
        # 流式回调键集与 chat() 一致：元数据 only，含 duration_ms + started_at
        assert set(captured) == {
            "model", "prompt_tokens", "completion_tokens", "total_tokens",
            "duration_ms", "started_at", "finish_reason",
        }
        assert captured["duration_ms"] >= 0
        assert captured["started_at"]

    @pytest.mark.asyncio
    async def test_degrades_when_stream_options_unsupported(self):
        # 端点不支持 stream_options（老兼容端点）→ 去掉该参数降级重试一次，
        # 重试成功后回调照常触发（token 缺失记 None，不中断流）
        client = make_client()
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content="hi", tool_calls=None)),
            _chunk(SimpleNamespace(role=None, content=None, tool_calls=None),
                   choices=[SimpleNamespace(
                       delta=SimpleNamespace(role=None, content=None, tool_calls=None),
                       finish_reason="stop")]),
        ]
        client.client.chat.completions.create.side_effect = [
            Exception("stream_options: Extra inputs are not permitted"),
            iter(chunks),
        ]
        captured = {}
        msg = await client.chat_stream(
            [Message(role="user", content="x")],
            telemetry_callback=lambda d: captured.update(d))
        calls = client.client.chat.completions.create.call_args_list
        assert "stream_options" in calls[0].kwargs
        assert "stream_options" not in calls[1].kwargs     # 重试不带该参数
        assert msg.content == "hi"
        assert captured["total_tokens"] is None            # 无 usage → token 留空


class _FakeResp:
    class _Choice:
        def __init__(self, finish_reason="stop"):
            self.finish_reason = finish_reason
            self.message = type("M", (), {"role": "assistant", "content": "hi",
                                          "tool_calls": None})()
    def __init__(self):
        self.choices = [self._Choice()]
        self.usage = type("U", (), {"prompt_tokens": 11, "completion_tokens": 7,
                                    "total_tokens": 18})()


def test_chat_calls_telemetry_callback():
    from paperflow.core.llm import LLMClient
    from paperflow.config import LLMConfig
    client = LLMClient(LLMConfig(api_key="x"))
    resp = _FakeResp()

    def fake_create(**kwargs):
        return resp

    client.client.chat.completions.create = fake_create
    captured = {}

    async def go():
        await client.chat([], telemetry_callback=lambda d: captured.update(d))

    import asyncio; asyncio.run(go())
    # 精确锁定 telemetry 键集：元数据 only（绝不含 content），与 audit.py
    # record_llm_call 的形参一一对应（Agent 以 **fields 转发）
    assert set(captured) == {
        "model", "prompt_tokens", "completion_tokens", "total_tokens",
        "duration_ms", "started_at", "finish_reason",
    }
    assert captured["model"] == client.model
    assert captured["total_tokens"] == 18
    assert captured["duration_ms"] >= 0            # monotonic 实测耗时
    # started_at 是调用起点墙钟 ISO，audit.py 靠它推算 ended_at → 必须可解析
    assert captured["started_at"]
    from datetime import datetime
    datetime.fromisoformat(captured["started_at"])
    assert captured["finish_reason"] == "stop"
