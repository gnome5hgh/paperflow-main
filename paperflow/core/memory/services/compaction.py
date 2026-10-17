"""上下文压缩：对话超窗时压缩 in-context 窗口（驱逐旧对话 + 插摘要）。

作用于 agent 的 in-context 窗口：保留头部 system + 冻结的历史摘要段 +
新摘要 + 近期尾部。只改窗口，绝不删 SQL 原始消息（Recall 完整保留可追溯）。

摘要对齐 Claude Code 的 9 节结构；其中「All user messages」由代码从 SQL
逐字插入，不让模型转述；旧摘要段冻结，只压新增段，避免递归重摘要与摘要段丢失。
"""
from __future__ import annotations

import json
import logging
from dataclasses import replace

from pydantic import BaseModel, Field

from paperflow.config.schemas.sections import CompactionSettings, ContextSize
from paperflow.core.llm import Message, SystemMessage, UserMessage
from paperflow.core.common.services.tokenization import get_token_encoder

__all__ = ["CompactionSettings", "ContextSize", "SummarySchema", "estimate_tokens",
           "blend_token_ratio", "should_compress", "run_compaction",
           "deterministic_trim", "format_task_anchor"]

logger = logging.getLogger(__name__)

#: 新版摘要的开场标记（Claude Code 的续接说明）；UserMessage 以此识别。
_SUMMARY_MARKER = (
    "This session is being continued from a previous conversation that ran out of context.")
#: 摘要的完整开场白；后面接 9 节正文。
_SUMMARY_OPENING = (
    f"{_SUMMARY_MARKER}\n"
    "The summary below covers the earlier portion of the conversation...\n\n"
)
#: 兼容旧版 SystemMessage 摘要的标记。
_LEGACY_SUMMARY_MARKER = "[对话摘要]"

#: 每条消息的协议开销（3 条消息框架 + 1 个角色名，对齐 OpenAI 的计数口径）。
_PROTOCOL_OVERHEAD = 4

#: 校准系数的夹紧区间与 EMA 权重：单次观测可能偏得离谱（缓存命中、端点口径差异），
#: 夹紧后折半并入，不让一次异常带偏整体估算。
_RATIO_MIN, _RATIO_MAX = 0.5, 2.0
_RATIO_WEIGHT = 0.5


class SummarySchema(BaseModel):
    """模型输出的 8 个摘要字段。

    第 6 节「All user messages」不在这里：它由代码从 SQL 逐字插入，避免模型
    转述用户原话或漏掉安全/授权类约束。

    Attributes:
        primary_request_and_intent: str，用户的核心请求与成功标准
        key_technical_concepts: str，关键技术概念与领域术语
        files_and_code_sections: str，涉及的论文/笔记/数据路径/脚本片段
        errors_and_fixes: str，错误与修复过程
        problem_solving: str，问题解决过程与决策依据
        pending_tasks: str，待办任务
        current_work: str，压缩前一刻正在做的事
        optional_next_step: str，与最近用户请求直接相关的下一步
    """

    primary_request_and_intent: str
    key_technical_concepts: str
    files_and_code_sections: str
    errors_and_fixes: str
    problem_solving: str
    pending_tasks: str
    current_work: str
    optional_next_step: str

    #: 任务锚点：与摘要同一次调用产出，随后由运行层单独持久化并每轮注入。
    goal: str = ""
    hard_constraints: list[str] = Field(default_factory=list)
    deadline: str | None = None
    unfinished_steps: list[str] = Field(default_factory=list)


def _is_summary(message: Message) -> bool:
    """判断一条消息是否是压缩摘要（新版 UserMessage 或旧版 SystemMessage）。

    Args:
        message: 待判断的消息。

    Returns:
        True 表示这是摘要消息，不参与触发计数、保留尾部与再次摘要。
    """
    content = message.content or ""
    if message.role == "user" and content.startswith(_SUMMARY_MARKER):
        return True
    return message.role == "system" and content.startswith(_LEGACY_SUMMARY_MARKER)


def estimate_tokens(messages: list[Message]) -> int:
    """粗估消息 token 总量：逐条算正文与工具调用，再加协议开销。

    协议开销固定 4（3 条消息框架 + 1 个角色名，对齐 OpenAI 的计数口径），不按角色
    分档——分档只会得到一桌拍出来的常数，真正的偏差在别处（工具调用的 JSON 此前
    完全没算，而 arguments 常是一整段内容）。

    视觉 content parts 只计其中的文本部分：图片 token 由模型侧按块决定，本地算不出来。

    Args:
        messages: 消息列表（Message 家族）。

    Returns:
        估算的总 token 数（整数）。
    """
    total = 0
    # 编码器单点在 core.common.services.tokenization（与 rag 切块共用同一口径）。
    enc = get_token_encoder()
    for m in messages:
        total += len(enc.encode(_countable_text(m))) + _PROTOCOL_OVERHEAD
        tool_calls = getattr(m, "tool_calls", None)
        if tool_calls:
            total += len(enc.encode(json.dumps(tool_calls, ensure_ascii=False)))
    return total


def _countable_text(m: Message) -> str:
    """取一条消息里可计数的文本：正文为字符串时就是它，content parts 时只取文本部分。

    Args:
        m: 一条消息。

    Returns:
        用于 token 计数的文本（图片等非文本部分为空）。
    """
    content = m.content
    if content is None or isinstance(content, str):
        return content or ""
    return "".join(part.get("text", "") for part in content
                   if isinstance(part, dict) and part.get("type") == "text")


def blend_token_ratio(current: float, actual_prompt_tokens: int | None,
                      estimated_prompt_tokens: int | None) -> float:
    """用一次真实用量校准「本地估算 → 云端实际」的比例（EMA + 夹紧）。

    本地估算只能逼近，偏差是系统性的（口径不同、工具定义不计入正文等），故拿上一次
    请求的真实 ``prompt_tokens`` 除以当时的本地估算，得到一个乘数。出现三种情况时
    原样返回当前系数：没有真实用量（端点不报、流式未开 include_usage）、没有当时的
    估算、或估算为 0。

    单次观测先夹紧在 [0.5, 2.0]——缓存命中、端点计数口径差异都可能让某一次偏得很远，
    不该让它带偏整体；再用 EMA 折半并入（内容结构每轮都在变，不直接把系数打到观测值）。

    Args:
        current: 当前系数（初始 1.0 = 不校准）。
        actual_prompt_tokens: 上一次请求端点上报的输入 token 数。
        estimated_prompt_tokens: 上一次请求发送前的本地估算。

    Returns:
        新的校准系数。
    """
    if not actual_prompt_tokens or not estimated_prompt_tokens:
        return current
    observed = actual_prompt_tokens / estimated_prompt_tokens
    observed = min(max(observed, _RATIO_MIN), _RATIO_MAX)
    return (1 - _RATIO_WEIGHT) * current + _RATIO_WEIGHT * observed


def should_compress(messages: list[Message], settings: CompactionSettings,
                    context_window: int, *, estimate: int | None = None,
                    ratio: float = 1.0) -> bool:
    """判断当前窗口是否该压缩：任一 trigger 条件满足即触发（OR 逻辑）。

    messages 类型数非摘要消息条数；tokens 直接比估算值；fraction 比
    ``value × 有效窗口``。不再叠加固定的 1.1 安全边际——余量由 fraction 承担。

    Args:
        messages: 当前 in-context 消息列表。
        settings: 压缩配置（含 trigger / keep 等）。
        context_window: 模型的实际上下文窗口大小（token 数）。
        estimate: 已算好的消息 token 估算；None 表示现算。
        ratio: 校准系数（见 ``blend_token_ratio``），1.0 = 沿用本地估算原值。

    Returns:
        True 表示应触发压缩，False 表示无需压缩。
    """
    if not settings.enabled:
        return False
    ctx_size = settings.resolve_context_size(context_window)
    if estimate is None:
        estimate = estimate_tokens(messages)
    effective = estimate * ratio
    non_summary_count = sum(1 for m in messages if not _is_summary(m))
    for trigger in settings.trigger:
        if trigger.type == "messages":
            if non_summary_count >= trigger.value:
                return True
        elif trigger.type == "tokens":
            if effective >= trigger.value:
                return True
        elif trigger.type == "fraction":
            if effective >= trigger.value * ctx_size:
                return True
    return False


async def run_compaction(messages: list[Message], settings: CompactionSettings,
                         llm, structured, summary_text: str | None = None,
                         context_window: int | None = None,
                         user_messages: list[str] | None = None,
                         summary_structured=None,
                         anchor_sink=None) -> list[Message] | None:
    """执行压缩：冻结旧摘要段 + 只压新增段 + 保留近期尾部。

    摘要文本可由调用方传入（测试/已有 summary），否则用 StructuredOutput 生成。
    摘要生成失败时返回 None，调用方应保持原窗口不变并重试。
    压缩只改 in-context 窗口；SQL 原始消息由 MessageManager 保留（Recall 可追溯）。

    Args:
        messages: 当前 in-context 消息列表（将被子集替换）。
        settings: 压缩配置（含 trigger / keep / 摘要参数等）。
        llm: LLM 客户端（未直接使用，保留供后续扩展）。
        structured: StructuredOutput 实例，用于生成结构化摘要。
        summary_text: 可选预生成的摘要正文；若为 None 则调用 _summarize 生成。
        context_window: 模型上下文窗口大小（用于计算 keep 的 fraction 预算）。
        user_messages: 该会话全部 user 消息的逐字文本；None 时从待摘要消息里取。
        summary_structured: 摘要专用 StructuredOutput；None 时用 structured。
        anchor_sink: 可选回调；摘要调用产出任务锚点时以 dict 形式回调一次。

    Returns:
        压缩后的新消息列表；摘要生成失败时返回 None。
    """
    if not settings.enabled:
        return None

    summaries = [m for m in messages if _is_summary(m)]
    # 兼容旧窗口里的真实 system 头部；摘要段不算头部。
    leading_system = [m for m in messages[:3]
                      if m.role == "system" and not _is_summary(m)][:1]
    leading_ids = {id(m) for m in leading_system}
    to_summarize = [m for m in messages
                    if not _is_summary(m) and id(m) not in leading_ids]
    if not to_summarize:
        return None

    anchor = None
    if summary_text is None:
        summarized = await _summarize(
            to_summarize, summary_structured or structured, settings,
            user_messages=user_messages)
        if summarized is None:
            return None
        summary_text, anchor = summarized
    if not summary_text:
        return None
    if anchor is not None and anchor_sink is not None:
        anchor_sink(anchor)

    tail = _recent_tail(to_summarize, settings, context_window)
    summary_message = UserMessage(content=_ensure_summary_envelope(summary_text))
    return leading_system + summaries + [summary_message] + tail


def deterministic_trim(messages: list[Message], settings: CompactionSettings,
                       context_window: int | None = None) -> list[Message] | None:
    """摘要连续失败且窗口超过硬上限时的确定性降级。

    保留全部冻结摘要段与配置的近期尾部，把中间被移出的消息数写成一条明确
    占位；不调 LLM，因此恢复不依赖另一次成功的模型调用。没有可移出的消息时
    返回 None。

    Args:
        messages: 当前 in-context 窗口。
        settings: 压缩配置（keep / context_size）。
        context_window: 模型上下文窗口（推导 0.8 预算）。

    Returns:
        降级后的窗口；没有可移出内容时返回 None。
    """
    summaries = [m for m in messages if _is_summary(m)]
    leading_system = [m for m in messages[:3]
                      if m.role == "system" and not _is_summary(m)][:1]
    leading_ids = {id(m) for m in leading_system}
    rest = [m for m in messages
            if not _is_summary(m) and id(m) not in leading_ids]
    ctx = settings.resolve_context_size(context_window or 0)
    budget = int(0.8 * ctx) if ctx > 0 else 0
    trim_settings = (replace(settings, keep=ContextSize("tokens", budget))
                     if budget > 0 else settings)
    tail = _recent_tail(rest, trim_settings, context_window)
    dropped = len(rest) - len(tail)
    if dropped <= 0:
        return None
    placeholder = SystemMessage(content=(
        f"[压缩失败：较早的 {dropped} 条消息已移出窗口；"
        "原文可用 conversation_search 回溯]"))
    return leading_system + summaries + [placeholder] + tail


def _ensure_summary_envelope(text: str) -> str:
    """确保摘要文本以 Claude Code 的开场白与识别标记开头。

    Args:
        text: 摘要正文，可能已经带开场白或旧版标记。

    Returns:
        带完整开场白的摘要内容。
    """
    if text.startswith(_SUMMARY_MARKER):
        return text
    body = text
    if body.startswith(_LEGACY_SUMMARY_MARKER):
        body = body[len(_LEGACY_SUMMARY_MARKER):].lstrip("\n")
    return f"{_SUMMARY_OPENING}{_LEGACY_SUMMARY_MARKER}\n{body}"


def _recent_tail(messages: list[Message], settings: CompactionSettings,
                 context_window: int | None = None) -> list[Message]:
    """按 keep 配置从后往前保留近期上下文。

    keep 支持三种计量：messages（条数）、tokens（绝对预算）、fraction
    （有效窗口比例）。两个关键约束不变：
    - 携带工具调用的 assistant 消息与其 tool 结果必须成对保留——孤立 tool 消息
      的 tool_call_id 无对应调用会触发 API 报错。
    - 成对逻辑之后再做一次孤儿清理兜底：tool 消息的 tool_call_id 必须在保留的
      assistant(tool_calls) 里找得到，否则丢弃（覆盖极端轨迹）。

    Args:
        messages: 待保留的消息列表（已排除摘要段与头部 system）。
        settings: 压缩配置（含 keep, context_size）。
        context_window: 模型上下文窗口（用于推导 fraction 预算）。

    Returns:
        保留的尾部消息列表（保持原有顺序）。
    """
    ctx_size = settings.resolve_context_size(context_window or 0)
    keep = settings.keep
    if keep.type == "messages":
        count_limit: int | None = int(keep.value)
        token_budget: int | None = None
    else:
        count_limit = None
        token_budget = (int(keep.value) if keep.type == "tokens"
                        else int(keep.value * ctx_size))

    kept: list[Message] = []
    used = 0
    count = 0
    for m in reversed(messages):
        # 跳过 system 消息与摘要段（摘要由调用方单独保留）
        if m.role == "system" or _is_summary(m):
            continue
        cost = estimate_tokens([m])
        if count_limit is not None:
            if count >= count_limit and kept:
                break
        elif token_budget is not None:
            if used + cost > token_budget and kept:
                break
        used += cost
        count += 1
        if any(m is k for k in kept):
            continue
        kept.append(m)

        # 若当前消息是 tool 消息，把关联的 assistant(tool_calls) 一并保留，
        # 保证 tool_call_id 在尾部里有对应调用。
        if m.role == "tool":
            for prev in reversed(messages[: messages.index(m)]):
                if prev.role == "assistant" and prev.tool_calls:
                    if prev not in kept:
                        kept.append(prev)
                    break

    result = list(reversed(kept))
    seen_tool_ids = {tc["id"] for m in result if m.role == "assistant" and m.tool_calls
                     for tc in m.tool_calls}
    return [m for m in result
            if not (m.role == "tool" and m.tool_call_id not in seen_tool_ids)]


_SUMMARY_PROMPT = """You are compressing a research assistant conversation into a structured summary.
Write in the user's language. Be specific: keep paper titles, file paths, tool names,
error messages, numbers, and decisions. Do not invent facts.

Do NOT write the "All user messages" section; the system inserts it verbatim.

Return these fields:
1. primary_request_and_intent — the user's core request and success criteria.
2. key_technical_concepts — key concepts and domain terms.
3. files_and_code_sections — papers, notes, data paths, and script snippets involved.
4. errors_and_fixes — errors encountered and how they were fixed.
5. problem_solving — the problem-solving process and the reasons behind decisions.
6. pending_tasks — what remains to be done.
7. current_work — what was happening immediately before this summary.
8. optional_next_step — the next step directly in line with the user's most recent request.

Also return the task anchor fields:
- goal — the user's core objective in one sentence.
- hard_constraints — a list of must/must-not constraints, copied verbatim from the user.
  Preserve security- and authorization-relevant instructions exactly.
- deadline — only if the user explicitly stated one; otherwise null. Never infer it.
- unfinished_steps — a list of steps that are not yet complete.

Keep the whole summary under {max_words} words.

Conversation to summarize:
<conversation>
{conversation}
</conversation>
"""


def _format_messages(messages: list[Message]) -> list[str]:
    """把待摘要消息格式化成可读行；user/assistant 截 2000 字符，tool 截 500。

    Args:
        messages: 待摘要的消息列表。

    Returns:
        每行一条消息的文本列表（保持原顺序）。
    """
    lines: list[str] = []
    for m in messages:
        content = (m.content or "").strip()
        if not content:
            continue
        label = m.role
        if m.role == "tool":
            # 指针消息只取预览：摘要不应携带 tool_call_id / 对象键等引用信息。
            try:
                pointer = json.loads(content)
                if isinstance(pointer, dict) and pointer.get("decision") == "persisted":
                    content = str(pointer.get("preview") or "")
            except (json.JSONDecodeError, TypeError):
                pass
            label = "tool"
        limit = 500 if m.role == "tool" else 2000
        if len(content) > limit:
            content = content[:limit] + "…"
        lines.append(f"{label}: {content}")
    return lines


def _trim_lines(lines: list[str], budget: int) -> list[str]:
    """按 token 预算从最旧一行开始丢弃，至少保留一行。

    Args:
        lines: 格式化后的消息行。
        budget: 输入 token 上限；<=0 表示不裁剪。

    Returns:
        裁剪后的行列表。
    """
    if budget <= 0 or not lines:
        return lines
    enc = get_token_encoder()
    while len(lines) > 1 and len(enc.encode("\n".join(lines))) > budget:
        lines.pop(0)
    return lines


def _truncate_words(text: str, max_words: int) -> str:
    """按 token 上限截断摘要（中文按 token 计，避免 split() 把整段当一个词）。

    Args:
        text: 摘要正文。
        max_words: token 上限（字段名为兼容保留）；<=0 表示不限制。

    Returns:
        截断后的正文。
    """
    if max_words <= 0:
        return text
    enc = get_token_encoder()
    tokens = enc.encode(text)
    if len(tokens) <= max_words:
        return text
    return enc.decode(tokens[:max_words]) + "\n\n…（摘要超过上限，已截断）"


def _render_summary(result: SummarySchema, user_messages: list[str],
                    max_words: int) -> str:
    """把 8 个模型字段与代码生成的用户消息拼成 9 节摘要。

    Args:
        result: 模型输出的 8 字段。
        user_messages: 该会话全部 user 消息的逐字文本。
        max_words: 全篇词数上限。

    Returns:
        9 节摘要正文（开场白由 run_compaction 统一包装）。
    """
    users = "\n".join(
        f"- {_truncate_chars(text.strip(), 2000)}"
        for text in user_messages if text and text.strip()
    ) or "（无）"
    sections = [
        ("1. Primary Request and Intent", result.primary_request_and_intent),
        ("2. Key Technical Concepts", result.key_technical_concepts),
        ("3. Files and Code Sections", result.files_and_code_sections),
        ("4. Errors and fixes", result.errors_and_fixes),
        ("5. Problem Solving", result.problem_solving),
        ("6. All user messages", users),
        ("7. Pending Tasks", result.pending_tasks),
        ("8. Current Work", result.current_work),
        ("9. Optional Next Step", result.optional_next_step),
    ]
    body = "\n\n".join(
        f"## {title}\n{text.strip() or '（无）'}" for title, text in sections)
    body = _truncate_words(body, max_words)
    return body


def _truncate_chars(text: str, limit: int) -> str:
    """按字符数截断文本，超限时附明确说明。

    Args:
        text: 原文。
        limit: 字符上限；<=0 表示不限制。

    Returns:
        截断后的文本。
    """
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + "\n…（已截断）"


def format_task_anchor(anchor: dict) -> str:
    """把任务锚点渲染成注入用的 system 块。

    硬约束逐字保留；截止时间只在用户明确说过时出现；整体限制在约 2000 字符。

    Args:
        anchor: 含 goal / hard_constraints / deadline / unfinished_steps 的 dict。

    Returns:
        渲染后的 `<task_anchor>` 文本；没有可用字段时返回空串。
    """
    goal = str(anchor.get("goal") or "").strip()
    constraints = [str(c).strip() for c in (anchor.get("hard_constraints") or [])
                   if str(c).strip()]
    deadline = str(anchor.get("deadline") or "").strip()
    steps = [str(s).strip() for s in (anchor.get("unfinished_steps") or [])
             if str(s).strip()]
    if not (goal or constraints or deadline or steps):
        return ""
    parts = ["<task_anchor>", "这是压缩前快照；当前对话优先。"]
    if goal:
        parts.append(f"目标：{goal}")
    if constraints:
        parts.append("硬约束：")
        parts.extend(f"- {c}" for c in constraints)
    if deadline:
        parts.append(f"截止时间：{deadline}")
    if steps:
        parts.append("未完成步骤：")
        parts.extend(f"- {s}" for s in steps)
    parts.append("</task_anchor>")
    return _truncate_chars("\n".join(parts), 2000)


async def _summarize(messages: list[Message], structured,
                     settings: CompactionSettings,
                     user_messages: list[str] | None = None
                     ) -> tuple[str, dict] | None:
    """生成 9 节摘要与任务锚点；失败或结果为空时返回 None。

    Args:
        messages: 待摘要的消息列表（已排除旧摘要与头部 system）。
        structured: StructuredOutput 实例（支持 extract 方法）。
        settings: 压缩配置（含 trim_tokens_to_summarize / summary_max_words）。
        user_messages: 该会话全部 user 消息；None 时从 messages 里取。

    Returns:
        (完整摘要文本, 任务锚点 dict)；失败时返回 None。
    """
    if structured is None:
        return None
    lines = _trim_lines(_format_messages(messages),
                        settings.trim_tokens_to_summarize)
    prompt = _SUMMARY_PROMPT.format(
        max_words=settings.summary_max_words,
        conversation="\n".join(lines) or "（无）",
    )
    try:
        result = await structured.extract(prompt=prompt, schema=SummarySchema)
    except Exception as exc:
        logger.warning("压缩摘要生成失败，窗口保持不变: %r", exc)
        return None
    summary_fields = (
        "primary_request_and_intent", "key_technical_concepts",
        "files_and_code_sections", "errors_and_fixes", "problem_solving",
        "pending_tasks", "current_work", "optional_next_step",
    )
    if not any(str(getattr(result, name)).strip() for name in summary_fields):
        logger.warning("压缩摘要为空，窗口保持不变")
        return None
    users = user_messages if user_messages is not None else [
        (m.content or "") for m in messages if m.role == "user"]
    anchor = {
        "goal": (result.goal or "").strip(),
        "hard_constraints": [c.strip() for c in result.hard_constraints
                             if c.strip()],
        "deadline": (result.deadline or "").strip() or None,
        "unfinished_steps": [s.strip() for s in result.unfinished_steps
                             if s.strip()],
    }
    return _render_summary(result, users, settings.summary_max_words), anchor
