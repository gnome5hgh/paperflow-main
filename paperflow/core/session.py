"""会话状态容器（Layer 4 跨轮状态，CLI 持有、传给 Supervisor）。

意图管线的 Stage 1 追问检测消费 prev_intent / prev_user_input（spec §4.6）；
跨轮澄清挂起消费 pending_intent（spec §6.2，≤2 轮）。

注：省略 ADR 提及的 recent_summaries——跨轮上下文由 ContextCompressor.summary
跨轮累计承担（CLI 复用同一 Supervisor 实例，compressor 常驻），Session 不冗余存。
"""
from dataclasses import dataclass

from paperflow.core.intent.intent_schema import IntentType


@dataclass
class PendingClarification:
    """跨轮澄清挂起状态（CLI 层持有，≤2 轮）。

    original_input：产生该澄清的输入（跨轮合并后文本，含已收集的澄清上下文）——
    超轮终止时以它为 best-guess 调度（比裸原输入更准）。
    round：已询问的澄清轮数。链式累计——REPL 重建时从旧值 +1，绝不重置为 0，
    否则 ≤2 轮上限形同虚设（D9）。round >= 2 走终止路径（force_dispatch）。
    """
    question: str
    original_input: str
    round: int = 0


@dataclass
class Session:
    """会话状态容器。prev_* 由 run() 结束后更新；pending_intent 由 CLI 维护。"""
    prev_intent: IntentType | None = None
    prev_user_input: str = ""
    pending_intent: PendingClarification | None = None
