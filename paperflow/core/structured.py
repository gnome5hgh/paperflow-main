# paperflow/core/structured.py
"""
StructuredOutput —— 通用结构化输出组件。

三层防御策略：

1. **生成约束**：json_mode（response_format=json_object）+ temperature=0 +
   关闭思维链（extra_body enable_thinking=False），从源头提高 JSON 正确率。
2. **验证重试**：pydantic 校验失败时，把上次输出 + 校验错误喂回 LLM
   带对照纠错重试，最多 ``max_retries`` 次。
3. **兜底**：重试耗尽时优先调用调用方提供的 ``fallback``，
   无 fallback 则抛 ``StructuredOutputError``。

Schema 递归展开：``_schema_to_prompt`` 将 pydantic 模型展开为字段级提示，
嵌套模型递归展开（``max_depth`` 限制深度），防止自引用模型无限递归。
"""

import json
import re
import types
from dataclasses import dataclass
from typing import Callable, get_origin, get_args, Union

from pydantic import BaseModel, ValidationError

from paperflow.core.llm import Message


@dataclass
class StructuredOutputConfig:
    max_retries: int = 2
    temperature: float = 0.0
    disable_thinking: bool = True
    json_mode: bool = True
    max_schema_depth: int = 3


class StructuredOutputError(Exception):
    """重试耗尽且无 fallback 时抛出。"""


class StructuredOutput:
    """三层防御：生成约束 → 验证重试（带对照纠错）→ 兜底。"""

    def __init__(self, llm, config: StructuredOutputConfig | None = None,
                 telemetry_callback=None):
        self.llm = llm
        self.config = config or StructuredOutputConfig()
        #: LLM 调用元数据回调(与 Agent 侧同语义,None = 零开销跳过):
        #: spawn 摘要提取的调用经此接审计,归属父 agent 的 trace/turn。
        self.telemetry_callback = telemetry_callback

    async def extract(self, prompt: str, schema: type[BaseModel],
                      fallback: Callable[[], BaseModel] | None = None) -> BaseModel:
        messages = [
            Message(role="system", content=(
                f"严格按以下 JSON 结构输出，不要附加任何文字：\n"
                f"{_schema_to_prompt(schema, max_depth=self.config.max_schema_depth)}"
            )),
            Message(role="user", content=prompt),
        ]
        last_error: Exception | None = None
        resp: Message | None = None
        attempts = 0

        for attempt in range(self.config.max_retries + 1):
            attempts = attempt + 1
            try:
                # telemetry_callback 仅在设置时才传:None 时保持原调用签名不变,
                # 既有调用方(fake llm 无此参数)零影响。
                chat_kwargs = dict(
                    json_mode=self.config.json_mode,
                    temperature=self.config.temperature,
                    extra_body={"enable_thinking": False} if self.config.disable_thinking else None,
                )
                if self.telemetry_callback is not None:
                    chat_kwargs["telemetry_callback"] = self.telemetry_callback
                resp = await self.llm.chat(messages, **chat_kwargs)
                data = json.loads(_extract_json_body(resp.content))
                result = schema(**data)
                return result
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = e
                messages.append(Message(role="user", content=(
                    f"你上次的输出：{(resp.content if resp else '')[:500]}\n"
                    f"校验失败：{e}\n"
                    f"请对照结构重新输出，不要附加任何说明文字。"
                )))

        if fallback is not None:
            return fallback()
        raise StructuredOutputError(
            f"结构化输出失败（{attempts} 次尝试）: {last_error}")


# ── 递归 Schema 展开 ──

def _is_model(annotation) -> bool:
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _type_to_str(annotation) -> str:
    origin = get_origin(annotation)
    if origin is list:
        inner = get_args(annotation)[0]
        return f"list[{_type_to_str(inner)}]"
    if _is_optional_union(origin):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _type_to_str(args[0]) + " (可空)"
    if annotation is str:
        return "string"
    if annotation is int:
        return "integer"
    if annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"
    if annotation is dict:
        return "object"
    return str(annotation).replace("typing.", "")


def _schema_to_prompt(schema: type[BaseModel], depth: int = 0,
                      max_depth: int = 3) -> str:
    pad = "  " * depth
    inner_pad = "  " * (depth + 1)
    parts = []
    for name, field in schema.model_fields.items():
        required = field.is_required()
        parts.append(inner_pad + _field_desc(name, field, depth, max_depth)
                     + (" (必填)" if required else " (可选)"))
    return "{\n" + "\n".join(parts) + f"\n{pad}}}"


def _field_desc(name: str, field, depth: int, max_depth: int) -> str:
    annotation = field.annotation
    inner, optional = _unwrap_optional(annotation)
    if _is_model(inner):
        return f"{name}: {_nested_body(inner, depth, max_depth)}" + _optional_suffix(optional)
    origin = get_origin(inner)
    if origin is list:
        elem = get_args(inner)[0]
        if _is_model(elem):
            return (f"{name}: list[{_nested_body(elem, depth, max_depth)}]"
                    + _optional_suffix(optional))
    return f"{name}: {_type_to_str(annotation)}"


def _unwrap_optional(annotation):
    """去掉 Optional[X] 包装，返回 (X, is_optional)。

    pydantic v2 的 model_fields 保留原始注解：PEP 604 的 ``X | None``
    其 origin 是 ``types.UnionType`` 而非 ``typing.Union``，两种都要兼容。
    """
    origin = get_origin(annotation)
    if _is_optional_union(origin):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_optional_union(origin) -> bool:
    return origin is Union or origin is types.UnionType


def _optional_suffix(optional: bool) -> str:
    return " (可空)" if optional else ""


def _nested_body(model: type[BaseModel], depth: int, max_depth: int) -> str:
    if depth + 1 > max_depth:
        return f"{model.__name__} {{...}}"
    return _schema_to_prompt(model, depth + 1, max_depth)


def _extract_json_body(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        parts = text.split("```", 2)
        if len(parts) >= 3:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        text = text[start:end + 1]
    return text.strip()
