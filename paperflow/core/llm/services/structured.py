# paperflow/core/llm/services/structured.py
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

from paperflow.core.llm.domain.dto.message import Message


@dataclass
class StructuredOutputConfig:
    """StructuredOutput 的行为参数：重试次数、LLM 采样温度、是否关思维链、
    是否强制 JSON 模式、schema 递归展开的最大深度。

    Attributes:
        max_retries: int，校验失败后的重试次数
        temperature: float，采样温度（0.0 = 确定性）
        disable_thinking: bool，是否关闭思维链（思维链尾随散文会破坏 JSON）
        json_mode: bool，是否强制 JSON 输出格式
        max_schema_depth: int，schema 递归展开的最大深度（防自引用模型无限递归）
    """

    max_retries: int = 2
    temperature: float = 0.0
    disable_thinking: bool = True
    json_mode: bool = True
    max_schema_depth: int = 3


class StructuredOutputError(Exception):
    """重试耗尽且无 fallback 时抛出。"""


class StructuredOutput:
    """三层防御：生成约束 → 验证重试（带对照纠错）→ 兜底。

    Attributes:
        llm: LLMClient，底层对话客户端
        config: StructuredOutputConfig，行为参数
        telemetry_callback: 回调 | None，LLM 调用元数据回调（None = 零开销跳过）
    """

    def __init__(self, llm, config: StructuredOutputConfig | None = None,
                 telemetry_callback=None):
        """装配 LLM 客户端与行为参数。

        Args:
            llm: LLMClient，底层对话客户端
            config: StructuredOutputConfig | None，行为参数（None 用默认）
            telemetry_callback: 回调 | None，LLM 调用元数据回调（归属父 agent 的 trace/turn）
        """
        self.llm = llm
        self.config = config or StructuredOutputConfig()
        #: LLM 调用元数据回调(与 Agent 侧同语义,None = 零开销跳过):
        #: spawn 摘要提取的调用经此接审计,归属父 agent 的 trace/turn。
        self.telemetry_callback = telemetry_callback

    async def extract(self, prompt: str, schema: type[BaseModel],
                      fallback: Callable[[], BaseModel] | None = None,
                      images: list[str] | None = None) -> BaseModel:
        """让 LLM 按给定 pydantic schema 稳定输出 JSON，并返回校验通过的实例。

        三层防御（从源头提正确率，到失败兜底）：
        1. 生成约束：json_mode 强制 JSON 格式 + 温度 0 确定性输出 + 关思维链
           （思维链容易尾随散文，破坏 JSON）。
        2. 验证重试 + 对照纠错：pydantic 校验失败时，把「上次输出前 500 字符 +
           校验错误」拼成新的 user 消息回喂给 LLM，让它对照错误修正——不是简单
           重试，而是把「你上次错在哪」显式给出，逼它在对照下改正。最多 max_retries 次。
        3. 兜底：重试耗尽优先调用调用方 ``fallback``；无 fallback 才抛
           ``StructuredOutputError``——「尽力而为，失败也要有明确出口」。

        Args:
            prompt: 给 LLM 的任务文本（要抽取什么、有哪些约束）
            schema: pydantic 模型类，既是校验目标，也经 _schema_to_prompt 展开成字段级提示喂给 LLM
            fallback: 重试耗尽时的兜底构造函数（无参返回 BaseModel 实例）；None 表示直接抛 StructuredOutputError
            images: 视觉分析的图片 data URL 列表；非 None 时 user 消息改为「文本 + 图」的 content parts，文本仍是 prompt



        Returns:
            校验通过的 schema 实例



        Raises:
            StructuredOutputError: 重试耗尽且无 fallback 时抛出

        """
        # 传了 images 时 user 消息改为 OpenAI content parts（文本 + 图片），
        # 让视觉模型能看图；未传则保持纯文本，旧行为不变。
        user_content = prompt if not images else [
            {"type": "text", "text": prompt},
            *({"type": "image_url", "image_url": {"url": url}} for url in images),
        ]
        messages = [
            Message(role="system", content=(
                f"严格按以下 JSON 结构输出，不要附加任何文字：\n"
                f"{_schema_to_prompt(schema, max_depth=self.config.max_schema_depth)}"
            )),
            Message(role="user", content=user_content),
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
    """annotation 是否是一个 pydantic 模型类（用于判断是否需递归展开）。

    Args:
        annotation: 任意类型注解

    Returns:
        True 表示是 pydantic 模型类（需递归展开）。
    """
    return isinstance(annotation, type) and issubclass(annotation, BaseModel)


def _type_to_str(annotation) -> str:
    """把字段的 Python 类型注解翻译成给 LLM 看的自然语言类型名。

    处理 list[X]（递归翻译内层）、``X | None``（标「可空」）以及基础类型映射
    （str→string、int→integer 等）；其余类型兜底用 repr 去掉 typing. 前缀。

    Args:
        annotation: 字段的 Python 类型注解

    Returns:
        给 LLM 看的自然语言类型名（如 string / list[integer] / X (可空)）。
    """
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
    """把 pydantic 模型展开成字段级 JSON 结构提示（喂给 LLM 的输出模板）。

    递归展开嵌套模型（经 _field_desc → _nested_body），用 ``max_depth`` 限制深度
    防自引用模型无限递归——这在 recursive 记忆 schema 上不是理论风险。
    每个字段标注必填/可选，缩进按深度递增，最终拼成一段类 JSON 文本。
    字段声明了 ``description`` 时附在其后：那是模型判断「这个字段何时该填」的唯一
    依据（代码里的注释进不了提示词），未声明 description 的字段输出与旧版一致。

    Args:
        schema: type[BaseModel]，待展开的模型类
        depth: int，当前递归深度（顶层 0）
        max_depth: int，递归展开上限

    Returns:
        类 JSON 的字段级结构提示文本（每字段标注必填/可选与 description）。
    """
    pad = "  " * depth
    inner_pad = "  " * (depth + 1)
    parts = []
    for name, field in schema.model_fields.items():
        required = field.is_required()
        line = (_field_desc(name, field, depth, max_depth)
                + (" (必填)" if required else " (可选)"))
        if field.description:
            # 说明压成单行：模板按行组织，多行说明会破坏缩进结构
            line += " — " + " ".join(field.description.split())
        parts.append(inner_pad + line)
    return "{\n" + "\n".join(parts) + f"\n{pad}}}"


def _field_desc(name: str, field, depth: int, max_depth: int) -> str:
    """生成单个字段的提示行：嵌套模型 / list[模型] 递归展开，其余走类型名翻译。

    先剥掉 Optional 包装拿到真实内层类型（_unwrap_optional），再判断是否需递归；
    Optional 语义通过 _optional_suffix 追加「(可空)」标注。

    Args:
        name: str，字段名
        field: pydantic FieldInfo，字段元信息
        depth: int，当前递归深度
        max_depth: int，递归展开上限

    Returns:
        该字段的提示行（嵌套模型/list[模型] 递归展开，其余翻译类型名，含可空标注）。
    """
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

    Args:
        annotation: 字段的原始类型注解

    Returns:
        (内层类型, 是否可空) 二元组。
    """
    origin = get_origin(annotation)
    if _is_optional_union(origin):
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return annotation, False


def _is_optional_union(origin) -> bool:
    """origin 是否为「可空联合类型」：PEP 604 的 ``X | None`` 其 origin 是
    ``types.UnionType`` 而非 ``typing.Union``，两种都要识别。

    Args:
        origin: typing.get_origin 的结果

    Returns:
        True 表示是可空联合类型（兼容 typing.Union 与 types.UnionType 两种 origin）。
    """
    return origin is Union or origin is types.UnionType


def _optional_suffix(optional: bool) -> str:
    """Optional 语义追加「(可空)」标注（True 才加）。

    Args:
        optional: bool，是否为可空类型

    Returns:
        可空时返回 " (可空)"，否则空串。
    """
    return " (可空)" if optional else ""


def _nested_body(model: type[BaseModel], depth: int, max_depth: int) -> str:
    """递归展开嵌套模型的主体；深度达到 max_depth 时只输出占位符 ``{...}``，
    防自引用模型无限递归。

    Args:
        model: type[BaseModel]，嵌套模型类
        depth: int，当前深度
        max_depth: int，递归展开上限

    Returns:
        嵌套模型展开文本；深度超限时返回占位符 {model} {...}。
    """
    if depth + 1 > max_depth:
        return f"{model.__name__} {{...}}"
    return _schema_to_prompt(model, depth + 1, max_depth)


def _extract_json_body(text: str) -> str:
    """从 LLM 回复中抽取纯 JSON 正文，容忍常见的「包裹」噪音。

    LLM 可能用 Markdown 代码围栏（```json ... ```）包裹 JSON，或前后缀散文；
    先剥掉首个代码围栏，再取首 ``{`` 到末 ``}`` 的子串。抽取不到（start==-1）
    时原样返回，让上层 json.loads 报错进入纠错重试路径。

    Args:
        text: str，LLM 的原始回复

    Returns:
        抽取到的 JSON 正文；抽不到时原样返回（交给上层 json.loads 报错进纠错重试）。
    """
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
