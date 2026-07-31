# paperflow/core/llm.py
import asyncio
from dataclasses import dataclass

from openai import OpenAI

from paperflow.config import LLMConfig


@dataclass
class Message:
    role: str
    content: str
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None


class LLMClient:
    def __init__(self, config: LLMConfig):
        self.client = OpenAI(base_url=config.base_url, api_key=config.api_key)
        self.model = config.model
        self.max_tokens = config.max_tokens
        self.temperature = config.temperature

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
        tool_choice: str = "auto",
    ) -> Message:
        kwargs = dict(
            model=self.model,
            messages=[_message_to_openai(m) for m in messages],
            max_tokens=self.max_tokens,
            temperature=self.temperature,
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice

        response = await asyncio.to_thread(
            self.client.chat.completions.create, **kwargs
        )
        choice = response.choices[0].message

        tool_calls = None
        if choice.tool_calls:
            tool_calls = [
                {
                    "id": tc.id,
                    "type": tc.type,
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in choice.tool_calls
            ]

        return Message(
            role=choice.role,
            content=choice.content or "",
            tool_calls=tool_calls,
        )


def _message_to_openai(m: Message) -> dict:
    msg: dict = {"role": m.role, "content": m.content}
    if m.tool_calls is not None:
        msg["tool_calls"] = m.tool_calls
    if m.tool_call_id is not None:
        msg["tool_call_id"] = m.tool_call_id
    return msg


def tool_to_openai_schema(t) -> dict:
    """Convert a Tool instance to OpenAI function-calling JSON Schema."""
    return {
        "type": "function",
        "function": {
            "name": t.name,
            "description": t.description,
            "parameters": t.parameters,
        },
    }
