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
    """有状态压缩器：summary 跨轮存活，每次 run() 注入。"""

    def __init__(self, config: ContextConfig, llm,
                 structured):   # structured 构造注入：与组装点顶层实例共享
        self.config = config
        self.llm = llm
        self.structured = structured
        self.summary: str | None = None     # 跨轮状态（Task 5 移除，过渡期保留）
        #: 跨轮累积的对话消息（合并方案唯一状态）。压缩后 history[0] 是 system
        #: 摘要消息，其余是未压缩的近期对话——history 整体作为下轮回放素材。
        self.history: list = []

    def _summary_text(self):
        """history[0] 若为 system（压缩产物摘要）则返回其文本，否则 None。

        增量压缩的输入（"基于已有摘要更新，不从零总结"）。摘要消息由 accumulate
        不 append system 保证稳坐 history[0]（见 spec §4.3）。
        """
        if self.history and self.history[0].role == "system":
            return self.history[0].content
        return None

    def accumulate(self, conv):
        """把一轮 run 的对话消息追加进 history（唯一写入口）。

        conv = run() 内旁路收集的本轮对话 [user, assistant(tool_calls), tool, ..., 最终 assistant]。
        追加前用身份比对跳过已在 history 尾部的消息——防御性双算防护（正常路径本轮
        消息尚未进 history，无重叠；中途压缩不改 conv，故身份比对极少命中，成本可忽略）。
        只 append 对话消息（user/assistant/tool），绝不 append system——保证摘要消息
        稳坐 history[0]。
        """
        tail_ids = {id(m) for m in self.history[-8:]}   # 只比对尾窗，防 O(n²)
        # 过滤 role == "system"：SKILL/摘要消息只作为头部注入，绝不进入累积
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

    async def compress(self, messages: list[Message]) -> list[Message]:
        """增量压缩：旧 summary（若有）+ 待压缩消息 → LLM SummarySchema → 三段重组。"""
        prompt = self._build_compression_prompt(messages, self.summary)
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

    async def compress_history(self) -> None:
        """压缩改写 history：旧对话折叠进 history[0] 摘要消息，近对话保留回放。

        触发方是 Agent.run 的 should_compress（见 spec §3.3）。增量压缩：old_summary
        （若有）作为输入，LLM 提取 SummarySchema 后生成新摘要消息，_split_tail 保留
        近对话（reserve_ratio）。失败降级：保留原始 history 不压缩——安全侧宁可多带
        token 也不丢对话（spec §5）。
        """
        from paperflow.core.llm import Message    # 惰性导入（llm → memory 循环依赖）

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
        """输入集 = 待压缩消息 + 已有摘要（若有）——增量压缩。

        old_summary 来自 history[0]（压缩产物摘要）或旧 self.summary，作为增量输入
        让 LLM 基于它更新而非从零总结。循环跳过 role=="system"——SKILL/旧摘要不是
        对话内容，不该混进压缩输入（摘要经 old_summary 块单独给出）。
        """
        parts = [self.config.compression_prompt]
        if old_summary:
            parts.append(f"\n已有摘要（基于它更新，不要从零总结）：\n{old_summary}")
        parts.append("\n\n对话内容：\n")
        for m in messages:
            if m.role == "system":
                continue
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
