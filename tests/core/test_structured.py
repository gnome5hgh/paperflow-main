# tests/test_structured.py
import pytest
from unittest.mock import MagicMock, AsyncMock
from pydantic import BaseModel, Field
from paperflow.core.structured import (
    StructuredOutput, StructuredOutputConfig, StructuredOutputError,
    _extract_json_body, _schema_to_prompt,
)
from paperflow.core.llm import Message


class FlatSchema(BaseModel):
    name: str
    count: int = Field(description="数量")


class NestedTask(BaseModel):
    agent: str
    priority: int


class NestedResult(BaseModel):
    intents: list[FlatSchema]
    schedule: list[NestedTask] | None = None


class RecursiveNode(BaseModel):
    name: str
    children: list["RecursiveNode"] = Field(default_factory=list)


def make_llm(responses: list[Message]):
    llm = MagicMock()
    async def chat(messages, tools=None, tool_choice="auto", json_mode=False,
                   temperature=None, extra_body=None):
        return responses.pop(0)
    llm.chat = chat
    llm.context_window = 65536
    return llm


class TestExtractJsonBody:
    def test_plain_json(self):
        assert _extract_json_body('{"a": 1}') == '{"a": 1}'

    def test_markdown_wrapped(self):
        text = '```json\n{"a": 1}\n```'
        assert _extract_json_body(text) == '{"a": 1}'

    def test_with_surrounding_text(self):
        text = 'Here is the result: {"a": 1} hope it helps'
        assert _extract_json_body(text) == '{"a": 1}'


class TestSchemaToPrompt:
    def test_flat_schema(self):
        out = _schema_to_prompt(FlatSchema)
        assert "name: string (必填)" in out
        assert "count: integer (必填)" in out

    def test_nested_schema_expanded(self):
        out = _schema_to_prompt(NestedResult)
        assert "list[" in out
        assert "agent: string (必填)" in out      # 嵌套展开到字段级

    def test_recursive_model_truncated(self):
        out = _schema_to_prompt(RecursiveNode)
        assert "children: list[RecursiveNode {...}]" in out or "RecursiveNode {...}" in out


class TestExtract:
    @pytest.mark.asyncio
    async def test_success_first_try(self):
        llm = make_llm([Message(role="assistant", content='{"name": "x", "count": 3}')])
        so = StructuredOutput(llm, config=StructuredOutputConfig(record_stats=False))
        result = await so.extract("task", FlatSchema)
        assert result.name == "x"
        assert result.count == 3

    @pytest.mark.asyncio
    async def test_retry_with_correction(self):
        llm = make_llm([
            Message(role="assistant", content='{"name": "x", "cnt": 3}'),   # 字段名错
            Message(role="assistant", content='{"name": "x", "count": 3}'),
        ])
        so = StructuredOutput(llm, config=StructuredOutputConfig(record_stats=False))
        result = await so.extract("task", FlatSchema)
        assert result.count == 3

    @pytest.mark.asyncio
    async def test_fallback_on_exhaustion(self):
        llm = make_llm([
            Message(role="assistant", content="not json at all"),
            Message(role="assistant", content="still not json"),
            Message(role="assistant", content="nope"),
        ])
        so = StructuredOutput(llm, config=StructuredOutputConfig(record_stats=False))
        result = await so.extract(
            "task", FlatSchema,
            fallback=lambda: FlatSchema(name="fallback", count=0),
        )
        assert result.name == "fallback"

    @pytest.mark.asyncio
    async def test_raises_without_fallback(self):
        llm = make_llm([
            Message(role="assistant", content="bad"),
            Message(role="assistant", content="bad"),
            Message(role="assistant", content="bad"),
        ])
        so = StructuredOutput(llm, config=StructuredOutputConfig(record_stats=False))
        with pytest.raises(StructuredOutputError):
            await so.extract("task", FlatSchema)

    @pytest.mark.asyncio
    async def test_passes_json_mode_and_temperature(self):
        captured = {}
        llm = MagicMock()
        async def chat(messages, tools=None, tool_choice="auto", json_mode=False,
                       temperature=None, extra_body=None):
            captured.update(json_mode=json_mode, temperature=temperature,
                            extra_body=extra_body)
            return Message(role="assistant", content='{"name": "x", "count": 1}')
        llm.chat = chat
        so = StructuredOutput(llm, config=StructuredOutputConfig(record_stats=False))
        await so.extract("task", FlatSchema)
        assert captured["json_mode"] is True
        assert captured["temperature"] == 0.0
        assert captured["extra_body"] == {"enable_thinking": False}

    @pytest.mark.asyncio
    async def test_telemetry_callback_reaches_llm_chat(self):
        """telemetry_callback 透传:StructuredOutput 收到的回调原样到达 llm.chat。

        摘要提取的 LLM 调用走这条链路接审计——回调没到 chat 就说明接线断了。
        """
        captured = {}
        llm = MagicMock()

        async def chat(messages, tools=None, tool_choice="auto", json_mode=False,
                       temperature=None, extra_body=None, telemetry_callback=None):
            captured["cb"] = telemetry_callback
            return Message(role="assistant", content='{"name": "x", "count": 1}')
        llm.chat = chat

        cb = lambda data: None
        so = StructuredOutput(llm, config=StructuredOutputConfig(record_stats=False),
                              telemetry_callback=cb)
        await so.extract("task", FlatSchema)
        assert captured["cb"] is cb
