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
        content, tool_calls, role = _accumulate_stream_chunks(chunks, deltas.append)
        assert content == "你好世界"
        assert role == "assistant"
        assert tool_calls is None
        assert deltas == ["你好", "世界"]

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
        content, tool_calls, role = _accumulate_stream_chunks(chunks, None)
        assert content == ""
        assert tool_calls == [
            {"id": "call_1", "type": "function",
             "function": {"name": "search_paper", "arguments": '{"query": "x"}'}},
            {"id": "call_2", "type": "function",
             "function": {"name": "read_file", "arguments": '{"path": "a"}'}},
        ]

    def test_skips_empty_choices_and_none_content(self):
        from paperflow.core.llm import _accumulate_stream_chunks
        deltas = []
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content="a", tool_calls=None)),
            _chunk(None, choices=[]),          # 空 choices chunk
            _chunk(SimpleNamespace(role=None, content=None, tool_calls=None)),  # None content
        ]
        content, _, _ = _accumulate_stream_chunks(chunks, deltas.append)
        assert content == "a"
        assert deltas == ["a"]                 # None content 不回调


class TestChatStream:
    @pytest.mark.asyncio
    async def test_passes_stream_true_and_accumulates(self):
        client = make_client()
        deltas = []
        chunks = [
            _chunk(SimpleNamespace(role="assistant", content="hi", tool_calls=None)),
            _chunk(SimpleNamespace(role=None, content="!", tool_calls=None)),
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
