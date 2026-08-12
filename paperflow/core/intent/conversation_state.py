"""跨轮会话状态容器:意图追问上下文与澄清挂起(CLI 持有、传给 Supervisor)。

意图管线的追问检测消费 prev_intent / prev_user_input(上一轮意图与输入);跨轮澄清
挂起消费 pending_intent(最多询问 2 轮)。跨轮上下文由上下文压缩器的历史累积承担
(CLI 复用同一 Supervisor 实例,压缩器常驻),本状态不冗余保存摘要。
"""
from dataclasses import dataclass

from paperflow.core.intent.schemas.intent import IntentType


@dataclass
class PendingClarification:
    """跨轮澄清挂起状态(CLI 层持有,最多 2 轮)。

    original_input:产生该澄清的输入(跨轮合并后的文本,含已收集的澄清上下文)——超轮
    终止时以它为最佳猜测调度,比裸原输入更准。
    round:已询问的澄清轮数,链式累计——重建时从旧值 +1,绝不重置为 0,否则轮数上限
    形同虚设;round >= 2 走终止路径(强制调度)。
    """
    question: str
    original_input: str
    round: int = 0


@dataclass
class ConversationState:
    """跨轮会话状态。prev_* 由 run() 结束后更新;pending_intent 由 CLI 维护。"""
    prev_intent: IntentType | None = None
    prev_user_input: str = ""
    pending_intent: PendingClarification | None = None
