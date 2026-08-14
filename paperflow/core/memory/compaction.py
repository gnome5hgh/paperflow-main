"""上下文压缩：对话超窗时压缩 in-context 窗口（驱逐旧对话 + 插摘要）。

作用于 agent.messages（in-context 窗口），用 sliding_window 模式把窗口压回
预算内：保留 head system 消息 + 结构化摘要 + 近期尾部。只改 in-context
窗口，绝不删 SQL 原始消息（Recall 完整保留可追溯）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from paperflow.core.llm import Message as WireMessage

__all__ = ["CompactionSettings", "SummarySchema", "should_compress", "run_compaction"]


class SummarySchema(BaseModel):
    """模型输出的结构化摘要字段（压缩时提取对话要点）。

    五个字段分别捕捉：用户核心请求、已完成进度、关键技术约束/决策/错误、
    待办优先级、以及必须保留的用户偏好与领域细节。
    """

    task_overview: str              # 用户核心请求与成功标准
    current_state: str              # 已完成进度
    important_discoveries: str      # 关键技术约束/决策/错误
    next_steps: str                 # 待办与优先级
    context_to_preserve: str        # 用户偏好/领域细节/承诺


class CompactionSettings:
    """压缩触发与保留预算配置。

    trigger_ratio 决定「多满才触发压缩」，reserve_ratio 决定「尾部保留多少
    预算」；context_size 显式给出时覆盖 model_window 的默认推导。
    """

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
        """返回实际使用的上下文窗口预算：显式配置优先，否则取模型窗口的一半。

        取一半是安全默认——LLM 上下文窗口还包含 system prompt 与记忆头等
        固定开销，全部按 model_window 预算会在估算时过早触发压缩。
        """
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
    """粗估消息 token 总量：每条内容 token 数 + 4 的协议开销常数。"""
    total = 0
    enc = _get_encoder()
    for m in messages:
        total += len(enc.encode(m.content or "")) + 4
    return total


def should_compress(messages: list[WireMessage], settings: CompactionSettings,
                    context_window: int) -> bool:
    """判断当前窗口是否该压缩：估算 token × 1.1 > trigger_ratio × 预算。

    1.1 是触发裕量：token 估算有误差，预留 10% 安全边际防撞硬窗口上限。
    """
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

    两个关键约束：
    - 携带工具调用的 assistant 消息与其 tool 结果必须成对保留——孤立 tool 消息
      的 tool_call_id 无对应调用会触发 API 报错。
    - 成对逻辑之后再做一次孤儿清理兜底：tool 消息的 tool_call_id 必须在保留的
      assistant(tool_calls) 里找得到，否则丢弃（覆盖极端轨迹）。
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
