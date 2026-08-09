"""上下文压缩：CompactionSettings + sliding_window 执行（取代 ContextCompressor）。

作用于 agent.messages（in-context 窗口）。触发保留 paperFlow 主动阈值；
压缩语义对齐 Letta sliding_window——驱逐旧对话 + index 1 插摘要。只改
in-context 窗口，绝不删 SQL 原始消息（Recall 完整保留可追溯）。
"""
from __future__ import annotations

from typing import Literal

from paperflow.core.llm import Message as WireMessage

__all__ = ["CompactionSettings", "should_compress", "run_compaction"]


class CompactionSettings:
    mode: Literal["sliding_window", "all_messages",
                  "self_compact_all", "self_compact_sliding_window"] = "sliding_window"
    trigger_ratio: float = 0.8
    reserve_ratio: float = 0.1
    context_size: int = 0

    def __init__(self, mode: str = "sliding_window", trigger_ratio: float = 0.8,
                 reserve_ratio: float = 0.1, context_size: int = 0):
        self.mode = mode
        self.trigger_ratio = trigger_ratio
        self.reserve_ratio = reserve_ratio
        self.context_size = context_size

    def resolve_context_size(self, model_window: int) -> int:
        if self.context_size > 0:
            return self.context_size
        return model_window // 2


_enc = None


def _get_encoder():
    """tiktoken 编码器模块级单例——多 Agent 实例/逐消息估算不重复加载 BPE 文件。"""
    global _enc
    if _enc is None:
        import tiktoken
        _enc = tiktoken.get_encoding("cl100k_base")
    return _enc


def _estimate_tokens(messages: list[WireMessage]) -> int:
    total = 0
    enc = _get_encoder()
    for m in messages:
        total += len(enc.encode(m.content or "")) + 4
    return total


def should_compress(messages: list[WireMessage], settings: CompactionSettings,
                    context_window: int) -> bool:
    ctx_size = settings.resolve_context_size(context_window)
    estimate = _estimate_tokens(messages)
    return estimate * 1.1 > settings.trigger_ratio * ctx_size


async def run_compaction(messages: list[WireMessage], settings: CompactionSettings,
                         llm, structured, summary_text: str | None = None,
                         context_window: int | None = None) -> list[WireMessage]:
    """执行压缩（sliding_window 默认）：驱逐旧对话 + index 1 插摘要 + 保留近期尾部。

    摘要文本可由调用方传入（测试/已有 summary），否则用 StructuredOutput 生成。
    压缩只改 in-context 窗口；SQL 原始消息由 MessageManager 保留（Recall 可追溯）。
    """
    if summary_text is None:
        summary_text = await _summarize(messages, structured)
    head = [m for m in messages[:3] if m.role == "system"][:1]
    if settings.mode in ("all_messages", "self_compact_all"):
        return head + [WireMessage(role="system", content=summary_text)]
    tail = _recent_tail(messages, settings, context_window)
    return head + [WireMessage(role="system", content=summary_text)] + tail


def _recent_tail(messages: list[WireMessage], settings: CompactionSettings,
                 context_window: int | None = None) -> list[WireMessage]:
    """从后往前保留对话到 reserve_ratio × context_size 预算。

    带工具调用的 assistant 消息与其 tool 结果成对保留（不允许孤立 tool 消息——
    tool_call_id 无对应调用会触发 API 报错）。成对逻辑之外再做一次孤儿清理兜底：
    极端轨迹（tool 引用的 id 在向前无任何 assistant 携带）下丢弃该 tool 消息。
    """
    ctx_size = settings.context_size
    if ctx_size <= 0:
        ctx_size = (context_window or 1000000) // 2
    budget = int(ctx_size * settings.reserve_ratio)
    kept: list[WireMessage] = []
    used = 0
    for m in reversed(messages):
        if m.role == "system":
            continue
        cost = _estimate_tokens([m])
        if used + cost > budget and kept:
            break
        used += cost
        if any(m is k for k in kept):
            continue
        kept.append(m)
        if m.role == "tool":
            for prev in reversed(messages[: messages.index(m)]):
                if prev.role == "assistant" and prev.tool_calls:
                    if prev not in kept:
                        kept.append(prev)
                    break
    result = list(reversed(kept))
    # 孤儿清理：tool 消息的 tool_call_id 必须能在保留的 assistant(tool_calls) 里找到
    seen_tool_ids = {tc["id"] for m in result if m.role == "assistant" and m.tool_calls
                     for tc in m.tool_calls}
    return [m for m in result if not (m.role == "tool" and m.tool_call_id not in seen_tool_ids)]


async def _summarize(messages, structured) -> str:
    """用 StructuredOutput + SummarySchema 生成结构化摘要文本。"""
    from paperflow.core.memory.context_config import SummarySchema  # 复用现有 schema
    prompt = "\n".join(f"{m.role}: {(m.content or '')[:2000]}" for m in messages
                       if m.role != "system")
    result = await structured.extract(
        prompt=prompt, schema=SummarySchema,
        fallback=lambda: SummarySchema(task_overview="", current_state="",
                                       important_discoveries="", next_steps="",
                                       context_to_preserve=prompt[:2000]))
    return ("[对话摘要]\n任务：{task_overview}\n进度：{current_state}\n"
            "发现：{important_discoveries}\n下一步：{next_steps}\n"
            "保留：{context_to_preserve}").format(**result.model_dump())
