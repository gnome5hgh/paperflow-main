# paperflow/terminal/repl/resume.py
"""会话恢复的历史回放：把 --resume 拿到的 in-context 窗口重新渲染进终端滚动区。

为什么需要：``--resume`` 恢复的是模型上下文（SQL 里 AgentState.message_ids 指的
那个窗口），屏幕上不留任何痕迹——用户看到空屏会以为恢复失败。
本模块把**同一个窗口**投影成 (role, text) 序列，用与 live 路径相同的渲染原语打出
去，让历史像「刚发生过」一样留在滚动区，用户上翻即可读到。

为什么必须走终态 print 通道而不是 rich Live：Live 是原地重绘，滚动区不留痕；
只有 console.print 系（renderer.print / print_markdown / print_raw）才是持久写入。

只读契约（关键）：回放只渲染，不 ``add_message``、不 ``update_agent``。任何一次
落盘都会把 message_ids 撑大，导致模型下一轮看到重复消息。

数据投影在此完成，主循环因此不必依赖 memory 层的 schema 类型；回放是 REPL 开场
的一次性动作，故随 repl 包分发给 loop.py 的装配点。
"""
from __future__ import annotations

from dataclasses import dataclass

from paperflow.core.security.text import sanitize_surrogates

__all__ = ["ResumeReplay", "build_resume_replay", "render_resume_replay"]

#: 参与回放的角色。tool 被排除：窗口里的 tool 消息只带工具结果 content，没有
#: agent_type，而 live 的工具行是 ``[supervisor] xxx`` 形态——重建出来是编造的。
#: system 被排除：它是 head 内部物（AGENT/SKILLS/记忆/INTENT 块），从未上过屏。
_REPLAYABLE_ROLES = ("user", "assistant")


@dataclass(frozen=True)
class ResumeReplay:
    """一次会话恢复的回放载荷（纯数据，terminal 层不依赖 memory schema）。

    Attributes:
        session_id: 会话标识（= messages 表的 agent_id）。
        entries: 按显示顺序排列的 ``(role, text)``；text 已清洗未配对代理。
        total: in-context 窗口的消息总数（含被 limit 截掉、被角色过滤掉的那些）。
        omitted: 因 limit 未参与回放的消息数（0 表示回放了整个窗口）。
        created_at: 会话创建时间（展示用，可为 None）。
    """

    session_id: str
    entries: list[tuple[str, str]]
    total: int
    omitted: int = 0
    created_at: str | None = None


def build_resume_replay(message_manager, session_id: str, *,
                        limit: int = 0, created_at: str | None = None) -> ResumeReplay:
    """读 in-context 窗口并投影为回放载荷（数据源与模型完全一致）。

    Args:
        message_manager: MessageManager。须已回填 ``agent_manager``，否则
            ``get_in_context_messages`` 降级为全量查询，窗口与模型不一致。
        session_id: 会话标识（即 messages 表的 agent_id）。
        limit: 只回放窗口末尾 N 条；<=0 表示回放整个窗口。
        created_at: 会话创建时间（展示用）。

    Returns:
        ResumeReplay。窗口为空时 ``entries`` 为空列表，调用方据此提示「暂无消息」
        而不是静默什么都不打（那会重演「以为没恢复」）。
    """
    window = message_manager.get_in_context_messages(session_id)
    total = len(window)
    omitted = 0
    if limit and limit > 0 and total > limit:
        omitted = total - limit
        window = window[-limit:]
    entries = []
    for m in window:
        role = str(getattr(m.role, "value", m.role))
        if role not in _REPLAYABLE_ROLES:
            continue
        # 老数据可能带未配对代理（写入清洗是后加的），rich 输出时会炸，这里兜一道
        entries.append((role, sanitize_surrogates(m.content or "")))
    return ResumeReplay(session_id=session_id, entries=entries, total=total,
                        omitted=omitted, created_at=created_at)


def render_resume_replay(renderer, replay: ResumeReplay) -> None:
    """把回放载荷逐条渲染进滚动区（同步纯打印，不调模型、不落盘）。

    渲染映射刻意与 live 路径对齐：用户消息用 ``❯ `` 前缀（prompt 回显的观感），
    assistant 消息走 Markdown（与流式回答落屏后的观感一致）。参与回放的只有这两个
    角色，判据见 _REPLAYABLE_ROLES。

    首尾各一条 dim 分隔行：头行说明恢复了哪个会话、多少条，尾行标出 live 的起点。
    逐条不加任何「历史」标记——用户要的是「像刚交互产生的一样」。

    Args:
        renderer: StreamRenderer（持锁打印，与并发流式事件串行）。
        replay: 由 build_resume_replay 产出的载荷。
    """
    head = f"── 已恢复会话 {replay.session_id}"
    if replay.created_at:
        head += f" · {replay.created_at}"
    head += f" · 上下文 {replay.total} 条"
    if replay.omitted:
        head += f"（仅回放最近 {len(replay.entries)} 条对话）"
    renderer.print(f"{head} " + "─" * 6, style="dim")

    if not replay.entries:
        renderer.print("  该会话暂无对话消息（窗口为空）", style="dim")

    for role, text in replay.entries:
        if role == "user":
            renderer.print_raw(f"❯ {text}")
        else:
            renderer.print_markdown(text)

    renderer.print("─" * 24 + " 以上为恢复的历史，下面继续你的任务 " + "─" * 6,
                   style="dim")
