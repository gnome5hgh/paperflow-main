# paperflow/core/memory/context_compressor.py
"""
ContextCompressor —— 对话上下文压缩器。

在对话累计超长时把历史消息压缩为结构化摘要，避免上下文窗口溢出：

1. **token 估算**：tiktoken（cl100k_base）估算每条消息的 token 数，
   每消息 +4 token 格式开销（OpenAI 每条消息的固定编码成本）。
2. **压缩触发**：``should_compress`` —— 估算 × 1.1 buffer 超过
   trigger_ratio × context_size 时返回 True。×1.1 是为了吸收
   tiktoken 对 DeepSeek 等模型的估算偏差，避免低估时实际超窗口。
3. **增量压缩**：旧摘要（若有）+ 待压缩消息 → LLM 结构化提取
   SummarySchema → 按 summary_template 格式化为新摘要。summary
   作为跨轮状态存活，每轮注入。
4. **三段重组**（``_rebuild_messages``）：头部 system（SKILL + MEMORY，
   只取前两条）+ 新摘要 + 尾部最近消息（``_split_tail``）。
   ``_split_tail`` 有成对约束：assistant(tool_calls) 与其 tool 结果
   必须成对保留或整对丢弃，绝不允许孤立 role="tool" 消息。
"""
from __future__ import annotations

from paperflow.core.memory.context_config import ContextConfig, SummarySchema


_enc = None


def _get_encoder():
    """tiktoken 编码器模块级单例——多 Agent 实例不重复加载 BPE 文件。"""
    global _enc
    if _enc is None:
        import tiktoken
        _enc = tiktoken.get_encoding("cl100k_base")
    return _enc


class ContextCompressor:
    """有状态压缩器：summary 跨轮存活，每次 run() 注入。"""

    def __init__(self, config: ContextConfig, llm,
                 structured):   # structured 构造注入：与组装点顶层实例共享
        self.config = config
        self.llm = llm
        self.structured = structured
        self.summary: str | None = None     # 跨轮状态

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

    async def compress(self, messages: list[Message]) -> list[Message]:
        """增量压缩：旧 summary（若有）+ 待压缩消息 → LLM SummarySchema → 三段重组。"""
        prompt = self._build_compression_prompt(messages)
        summary = await self.structured.extract(
            prompt=prompt,
            schema=SummarySchema,
            fallback=lambda: SummarySchema(
                task_overview="", current_state="",
                important_discoveries="", next_steps="",
                # 只保留对话部分（_build_compression_prompt 在"对话内容："标记后），
                # 不把压缩指令本身存进 context_to_preserve
                context_to_preserve=prompt.split("对话内容：")[-1][:2000],
            ),
        )
        old_summary = self.summary      # 重建前保存旧值：_rebuild_messages 排除旧摘要用
        self.summary = self.config.summary_template.format(**summary.model_dump())
        return self._rebuild_messages(messages, old_summary)

    def _build_compression_prompt(self, messages: list[Message]) -> str:
        """输入集 = 待压缩消息 + 现有 self.summary（若有）——增量压缩。"""
        parts = [self.config.compression_prompt]
        if self.summary:
            parts.append(f"\n已有摘要（基于它更新，不要从零总结）：\n{self.summary}")
        parts.append("\n\n对话内容：\n")
        for m in messages:
            parts.append(f"{m.role}: {m.content[:2000]}")
        return "\n".join(parts)

    def _split_tail(self, messages: list[Message], ratio: float) -> list[Message]:
        """从后往前截到 ratio × context_size。

        约束1：tail 不含 system 消息 —— 头部 system（SKILL + MEMORY）
        与新摘要由 _rebuild_messages 负责，旧摘要绝不与尾部并存。
        约束2：assistant(tool_calls) 与其 tool 结果必须成对保留或整对丢弃——
        绝不允许孤立 role="tool" 消息（tool_call_id 无对应 → API 报错）。
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
                continue    # 配对循环已补回该 assistant，跳过重复追加（用身份比较——
                            # Message 值相等性会把内容相同的两条真实消息误判为重复）
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

    def _rebuild_messages(self, messages: list[Message],
                          old_summary: str | None = None) -> list[Message]:
        """三段式重组：头部 system（SKILL + MEMORY，排除旧摘要）+ 新 summary + 尾部。

        old_summary = 压缩前的 self.summary。无 MEMORY.md 时 messages 前三条是
        [SKILL, 旧摘要, ...]，若不做排除，旧摘要在头部占位存活 → [SKILL, 旧, 新] 并存。
        注意时序：compress() 在调用前已把 self.summary 更新为新摘要，故排除必须
        用重建前保存的旧值，不能用 self.summary 现值比较。
        """
        # 惰性导入 Message：llm → config → memory 包初始化链存在循环依赖，
        # 模块顶层导入会在 llm 未完成初始化时触发 ImportError
        from paperflow.core.llm import Message
        head = [m for m in messages[:3]
                if m.role == "system" and m.content != old_summary][:2]
        tail = self._split_tail(messages, ratio=self.config.reserve_ratio)
        return head + [
            Message(role="system", content=self.summary or ""),
        ] + tail
