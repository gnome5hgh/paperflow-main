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
