# paperflow/core/memory/context_compressor.py
"""
对话上下文压缩器：在对话累计超长时把历史消息压缩为结构化摘要，避免超出模型窗口。

整体流程围绕一个跨轮累积的 ``history`` 列表展开：

1. **token 估算**：用 tiktoken（cl100k_base）估算每条消息的 token 数，
   每条消息再加 4 token 的格式开销（模型按条计数的固定成本）。
2. **压缩触发**：``should_compress`` 在“估算值 × 1.1 超过 触发比例 × 窗口大小”时
   返回真。×1.1 的余量用于吸收估算偏差，避免低估 token 数导致实际超窗。
3. **跨轮累积**：``accumulate`` 在每轮对话执行结束后把对话消息追加进 history，
   是唯一的写入入口（不追加 system 消息），history 是压缩器唯一的跨轮状态。
4. **压缩改写**：``compress_history`` 在 history 超出保留预算时做增量压缩——
   若 history[0] 是上轮摘要（system 消息）则取它作输入，让模型提取结构化摘要后
   写回 history[0]，再通过 ``_split_tail`` 保留近期对话供下轮重放。history 未超
   预算时直接返回不调模型（避免无净收益的空转压缩）。失败时保留原始 history
   不压缩——宁可多带 token 也不丢对话。``_split_tail`` 有成对约束：带工具调用的
   assistant 消息与其 tool 结果必须成对保留或整对丢弃，绝不允许孤立 tool 消息。
"""
from __future__ import annotations

import logging

from paperflow.core.memory.context_config import ContextConfig, SummarySchema


logger = logging.getLogger(__name__)

_enc = None


def _get_encoder():
    """tiktoken 编码器模块级单例——多 Agent 实例不重复加载 BPE 文件。"""
    global _enc
    if _enc is None:
        import tiktoken
        _enc = tiktoken.get_encoding("cl100k_base")
    return _enc


class ContextCompressor:
    """有状态压缩器：history 跨轮累积，压缩后摘要消息稳坐 history[0]。"""

    def __init__(self, config: ContextConfig, llm,
                 structured):   # structured 构造注入：与组装点顶层实例共享
        self.config = config
        self.llm = llm
        self.structured = structured
        #: 跨轮累积的对话消息（压缩器唯一的跨轮状态）。压缩后 history[0] 是 system
        #: 摘要消息，其余是未压缩的近期对话——history 整体作为下轮重放的素材。
        self.history: list = []

    def _summary_text(self):
        """返回 history[0] 的摘要文本；若 history[0] 不是 system 消息则返回 None。

        这是增量压缩的输入：让模型基于已有摘要更新，而不是从零重新总结。摘要消息
        能稳坐 history[0]，是因为 accumulate 只追加对话消息、绝不追加 system。
        """
        if self.history and self.history[0].role == "system":
            return self.history[0].content
        return None

    def accumulate(self, conv):
        """把一轮对话执行的消息追加进 history（唯一的写入入口）。

        conv 是在本轮对话执行过程中旁路收集的消息序列
        [user, assistant(带工具调用), tool, ..., 最终 assistant]。
        追加前用对象身份比对跳过已在 history 尾部的消息，作为防御性的防重复追加
        （正常路径下本轮消息尚未进 history，没有重叠；中途压缩不会改动 conv，
        因此身份比对几乎不会命中，成本可忽略）。只追加对话消息（user/assistant/tool），
        绝不追加 system——这样才能保证摘要消息稳坐 history[0]。
        """
        tail_ids = {id(m) for m in self.history[-8:]}   # 只比对尾窗，避免 O(n²)
        # 过滤 system 消息：角色提示词与摘要只作为头部注入，绝不进入累积状态
        self.history.extend(m for m in conv
                            if m.role != "system" and id(m) not in tail_ids)

    def _estimate_tokens(self, messages: list[Message]) -> int:
        """tiktoken 估算总 token（每消息 +4 token 格式开销）。"""
        enc = _get_encoder()
        total = 0
        for m in messages:
            total += len(enc.encode(m.content or "")) + 4
        return total

    def should_compress(self, messages: list[Message]) -> bool:
        ctx_size = self.config.resolve_context_size(self.llm.context_window)
        estimate = self._estimate_tokens(messages)
        # ×1.1 buffer：tiktoken 对 DeepSeek 的估算偏差 → 低估时不至于实际超窗口
        return estimate * 1.1 > self.config.trigger_ratio * ctx_size

    async def compress_history(self) -> None:
        """压缩改写 history：旧对话折叠进 history[0] 摘要消息，近期对话保留重放。

        由 Agent.run 在 should_compress 判定超限时调用。增量压缩：把 old_summary
        （若有）作为输入，让模型按 SummarySchema 提取出新摘要并写入 history[0]，
        再用 _split_tail 按 reserve_ratio 保留近期对话。失败时保留原始 history
        不压缩——安全侧宁可多带 token 也不丢对话。
        """
        from paperflow.core.llm import Message    # 惰性导入（llm 与 memory 存在循环依赖）

        # 无净收益防护：history 已 ≤ reserve 预算时直接返回，不调模型、不改 history。
        # 压缩产物（摘要 + 近期对话）设计上只会收缩到 ~reserve，此时再压缩只会产生
        # 无意义摘要。典型场景是单轮对话异常巨大（大工具结果、多轮迭代）导致
        # should_compress 反复判定超限、而 history 本身很小——每次迭代白跑一次结构化
        # 提取，还会把噪音摘要写进 history[0] 污染下轮输入。
        # reserve 预算 = 上下文窗口 × reserve_ratio（默认 32K × 0.1 ≈ 3276 token）。
        reserve = int(self.config.resolve_context_size(self.llm.context_window)
                      * self.config.reserve_ratio)
        if self._estimate_tokens(self.history) <= reserve:
            return

        old_summary = self._summary_text()
        prompt = self._build_compression_prompt(self.history, old_summary)
        try:
            summary = await self.structured.extract(
                prompt=prompt,
                schema=SummarySchema,
                fallback=lambda: SummarySchema(
                    task_overview="", current_state="",
                    important_discoveries="", next_steps="",
                    context_to_preserve=prompt.split("对话内容：")[-1][:2000],
                ),
            )
            summary_text = self.config.summary_template.format(**summary.model_dump())
        except Exception:
            logger.warning("compress_history failed, keeping raw history", exc_info=True)
            return

        tail = self._split_tail(self.history, ratio=self.config.reserve_ratio)
        self.history = [Message(role="system", content=summary_text)] + tail

    def _build_compression_prompt(self, messages: list[Message], old_summary=None) -> str:
        """拼出压缩提示词：待压缩消息 + 已有摘要（若有）——实现增量压缩。

        old_summary 取自 history[0]（上一轮压缩产出的摘要），作为增量输入让模型
        基于它更新而不是从零总结。循环跳过 system 消息——角色提示词与旧摘要不属于
        对话内容，不该混进压缩输入（摘要已通过 old_summary 一段单独给出）。
        """
        parts = [self.config.compression_prompt]
        if old_summary:
            parts.append(f"\n已有摘要（基于它更新，不要从零总结）：\n{old_summary}")
        parts.append("\n\n对话内容：\n")
        for m in messages:
            if m.role == "system":
                continue
            # content 可能为 None（带工具调用的 assistant 消息）。本方法在
            # compress_history 的异常捕获之外调用，这里抛错会中断整个对话执行，
            # 因此取空串兜底，保证压缩路径永远能构造出提示词。
            parts.append(f"{m.role}: {(m.content or '')[:2000]}")
        return "\n".join(parts)

    def _split_tail(self, messages: list[Message], ratio: float) -> list[Message]:
        """从后往前截取近期对话，直到总 token 达到 ratio × context_size。

        约束 1：尾部不含 system 消息——角色提示词/摘要由头部注入，compress_history
        把新摘要写入 history[0]，旧摘要绝不与尾部并存。
        约束 2：带工具调用的 assistant 消息与其 tool 结果必须成对保留或整对丢弃，
        绝不允许出现孤立的 tool 消息（其 tool_call_id 找不到对应调用会触发 API 报错）。
        """
        ctx_size = self.config.resolve_context_size(self.llm.context_window)
        budget = int(ctx_size * ratio)
        kept: list[Message] = []
        used = 0
        for m in reversed(messages):
            if m.role == "system":
                continue    # system 由头部/新摘要处理，不进 tail
            cost = self._estimate_tokens([m])
            if used + cost > budget and kept:
                break
            used += cost
            if any(m is k for k in kept):
                continue    # 配对逻辑已把该 assistant 补回，这里跳过重复追加。用身份比较
                            # 而非值比较，因为内容相同的两条真实消息会被值相等误判为重复
            kept.append(m)
            if m.role == "tool":
                # 向前补回对应的 assistant(tool_calls) 消息（成对约束）
                for prev in reversed(messages[: messages.index(m)]):
                    if prev.role == "assistant" and prev.tool_calls:
                        if prev not in kept:
                            kept.append(prev)
                        break
        result = list(reversed(kept))
        # 后置清理：若尾部残留孤立 tool 消息（其 assistant 没进来），丢弃
        seen_tool_ids = {tc["id"] for m in result if m.role == "assistant" and m.tool_calls for tc in m.tool_calls}
        return [m for m in result if not (m.role == "tool" and m.tool_call_id not in seen_tool_ids)]
