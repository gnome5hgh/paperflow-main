"""上下文压缩：对话超窗时压缩 in-context 窗口（驱逐旧对话 + 插摘要）。

作用于 agent.messages（in-context 窗口），用 sliding_window 模式把窗口压回
预算内：保留 head system 消息 + 结构化摘要 + 近期尾部。只改 in-context
窗口，绝不删 SQL 原始消息（Recall 完整保留可追溯）。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from paperflow.core.llm import Message as WireMessage
from paperflow.core.tokenization import get_token_encoder

__all__ = ["CompactionSettings", "SummarySchema", "should_compress", "run_compaction"]


class SummarySchema(BaseModel):
    """模型输出的结构化摘要字段（压缩时提取对话要点）。

    五个字段分别捕捉：用户核心请求、已完成进度、关键技术约束/决策/错误、
    待办优先级、以及必须保留的用户偏好与领域细节。

    Attributes:
        task_overview: str，用户的核心请求与成功标准
        current_state: str，已完成的进度
        important_discoveries: str，关键技术约束/决策/错误
        next_steps: str，待办事项与优先级
        context_to_preserve: str，必须保留的用户偏好与领域细节
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

    Attributes:
        mode: Literal，压缩模式；"sliding_window" 保留头部+摘要+尾部，其余三值见 __init__
        trigger_ratio: float，触发压缩的 token 使用率阈值（0~1）
        reserve_ratio: float，压缩后尾部保留的预算比例（0~1）
        context_size: int，显式上下文预算（token）；0 表示由模型窗口推导
    """

    mode: Literal["sliding_window", "all_messages",
                  "self_compact_all", "self_compact_sliding_window"] = "sliding_window"
    trigger_ratio: float = 0.8
    reserve_ratio: float = 0.1
    context_size: int = 0

    def __init__(self, mode: str = "sliding_window", trigger_ratio: float = 0.8,
                 reserve_ratio: float = 0.1, context_size: int = 0):
        """初始化压缩配置。

        Args:
            mode: 压缩模式：
                - "sliding_window": 保留头部 system + 摘要 + 近期尾部（默认）。
                - "all_messages": 整个上下文压成 system + 单条摘要（无尾部）。
                - "self_compact_all": 同 all_messages，语义标注。
                - "self_compact_sliding_window": 同 sliding_window。
            trigger_ratio: 触发压缩的 token 使用率阈值（0~1），默认 0.8。
            reserve_ratio: 压缩后尾部保留的预算比例（0~1），默认 0.1。
            context_size: 显式指定上下文预算（token 数），若为 0 则由模型窗口推导。
        """
        self.mode = mode
        self.trigger_ratio = trigger_ratio
        self.reserve_ratio = reserve_ratio
        self.context_size = context_size

    def resolve_context_size(self, model_window: int) -> int:
        """返回实际使用的上下文窗口预算：显式配置优先，否则取模型窗口的一半。

        取一半是安全默认——LLM 上下文窗口还包含 system prompt 与记忆头等
        固定开销，全部按 model_window 预算会在估算时过早触发压缩。

        Args:
            model_window: 模型的最大上下文窗口大小（token 数）。

        Returns:
            压缩判定和保留预算使用的有效上下文大小。
        """
        if self.context_size > 0:
            return self.context_size
        return model_window // 2


def _estimate_tokens(messages: list[WireMessage]) -> int:
    """粗估消息 token 总量：每条内容 token 数 + 4 的协议开销常数。

    Args:
        messages: 消息列表（WireMessage 格式）。

    Returns:
        估算的总 token 数（整数）。
    """
    total = 0
    # 编码器单点在 core.tokenization（与 rag 切块共用同一口径）。
    enc = get_token_encoder()
    for m in messages:
        total += len(enc.encode(m.content or "")) + 4
    return total


def should_compress(messages: list[WireMessage], settings: CompactionSettings,
                    context_window: int) -> bool:
    """判断当前窗口是否该压缩：估算 token × 1.1 > trigger_ratio × 预算。

    1.1 是触发裕量：token 估算有误差，预留 10% 安全边际防撞硬窗口上限。

    Args:
        messages: 当前 in-context 消息列表。
        settings: 压缩配置（含 trigger_ratio 等）。
        context_window: 模型的实际上下文窗口大小（token 数）。

    Returns:
        True 表示应触发压缩，False 表示无需压缩。
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

    Args:
        messages: 当前 in-context 消息列表（将被子集替换）。
        settings: 压缩配置（含 mode, reserve_ratio 等）。
        llm: LLM 客户端（未直接使用，保留供后续扩展）。
        structured: StructuredOutput 实例，用于生成结构化摘要。
        summary_text: 可选预生成的摘要文本；若为 None 则调用 _summarize 生成。
        context_window: 模型上下文窗口大小（用于计算保留预算），若为 None 则用 settings.context_size。

    Returns:
        压缩后的新消息列表（system 头部 + 摘要消息 + 尾部消息）。

    压缩结果结构：
        - 模式 "sliding_window" 或 "self_compact_sliding_window":
          [system_head] + [摘要消息(role=system)] + [近期尾部消息]
        - 模式 "all_messages" 或 "self_compact_all":
          [system_head] + [摘要消息(role=system)]
    """
    # 1. 若未提供摘要文本，则调用 LLM 生成结构化摘要
    if summary_text is None:
        summary_text = await _summarize(messages, structured)

    # 2. 提取头部 system 消息（仅保留第一条 system 消息）
    #    从消息列表前 3 条中筛选 system 角色，取第一个（通常为 agent 的系统提示）
    head = [m for m in messages[:3] if m.role == "system"][:1]

    # 3. 根据模式决定是否保留尾部
    #    all_messages / self_compact_all 模式：完全压缩，无尾部保留
    if settings.mode in ("all_messages", "self_compact_all"):
        return head + [WireMessage(role="system", content=summary_text)]

    # 4. sliding_window 模式：保留尾部（近期对话）
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

    Args:
        messages: 当前完整消息列表。
        settings: 压缩配置（含 reserve_ratio, context_size）。
        context_window: 模型上下文窗口（用于推导 budget）。

    Returns:
        保留的尾部消息列表（保持原有顺序）。
    """
    # 1. 计算尾部保留的 token 预算
    ctx_size = settings.context_size
    if ctx_size <= 0:
        ctx_size = (context_window or 1000000) // 2  # 默认大值防除零
    budget = int(ctx_size * settings.reserve_ratio)

    # 2. 从后往前遍历，累积保留消息直至预算耗尽
    kept: list[WireMessage] = []
    used = 0
    for m in reversed(messages):
        # 跳过 system 消息（已由头部处理）
        if m.role == "system":
            continue
        # 估算当前消息 token 数
        cost = _estimate_tokens([m])
        # 若超过预算且已有保留消息，则停止（至少保留一条尾部消息）
        if used + cost > budget and kept:
            break
        used += cost
        # 避免重复添加（极少数情况）
        if any(m is k for k in kept):
            continue
        kept.append(m)

        # 3. 若当前消息是 tool 消息，则尝试将其关联的 assistant（含 tool_calls）也保留
        #    这是为了保证 tool_call_id 的上下文完整性。
        if m.role == "tool":
            # 从原消息列表中向前查找该 tool 对应的 assistant 消息
            # 注意：由于是 revers 遍历，这里获取索引要注意，但这里直接用 messages.index
            # 这可能 O(n^2)，但尾部规模通常很小，可接受。
            for prev in reversed(messages[: messages.index(m)]):
                if prev.role == "assistant" and prev.tool_calls:
                    if prev not in kept:
                        kept.append(prev)
                    break  # 仅关联最近的 assistant

    # 4. 反转为正向顺序
    result = list(reversed(kept))

    # 5. 孤儿清理：对于保留的 tool 消息，检查其 tool_call_id 是否能被保留的 assistant 中的调用匹配
    #    若无法匹配则丢弃该 tool 消息（防止 API 报错）
    seen_tool_ids = {tc["id"] for m in result if m.role == "assistant" and m.tool_calls
                     for tc in m.tool_calls}
    return [m for m in result if not (m.role == "tool" and m.tool_call_id not in seen_tool_ids)]


async def _summarize(messages, structured) -> str:
    """用 StructuredOutput + SummarySchema 生成结构化摘要文本。

    Args:
        messages: 要摘要的消息列表（通常为完整窗口消息）。
        structured: StructuredOutput 实例（支持 extract 方法）。

    Returns:
        格式化的摘要文本（包含五个字段）。

    实现细节：
        - 构造 prompt：将每条消息的 role 和 content（截断至 2000 字符）拼接。
        - 使用 SummarySchema 约束输出结构。
        - 若结构化提取失败，fallback 生成包含原始 prompt 前 2000 字符的摘要。
    """
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