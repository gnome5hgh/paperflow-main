"""一次 LLM 调用的 token 用量。"""
from dataclasses import dataclass

__all__ = ["Usage"]


@dataclass(frozen=True)
class Usage:
    """一次调用的 token 用量。

    端点未必报全：部分兼容端点只给总数，流式路径更常整段缺失（要端点支持
    ``stream_options.include_usage`` 才有），故每一项都可为空而非取默认 0——
    「没报」与「报了 0」是两件事。

    Attributes:
        prompt_tokens: 输入 token 数
        completion_tokens: 输出 token 数
        total_tokens: 总 token 数（端点自报值，与前两项之和不一致时以它为准）
        cached_tokens: 输入中命中前缀缓存的 token 数
            （响应里的 ``prompt_tokens_details.cached_tokens``）
        reasoning_tokens: 输出中用于思维链的 token 数
            （响应里的 ``completion_tokens_details.reasoning_tokens``）
    """

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    cached_tokens: int | None = None
    reasoning_tokens: int | None = None
