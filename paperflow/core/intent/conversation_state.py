"""跨轮会话状态容器:意图追问上下文与澄清挂起(CLI 持有、传给 Supervisor)。

意图管线的追问检测消费 prev_intent / prev_user_input(上一轮意图与输入);跨轮澄清
挂起消费 pending_intent(最多 PendingClarification.MAX_ROUNDS 轮)。跨轮上下文由
上下文压缩器的历史累积承担(CLI 复用同一 Supervisor 实例,压缩器常驻),本状态不
冗余保存摘要。
"""
from dataclasses import dataclass
from typing import ClassVar

from paperflow.core.intent.schemas.intent import IntentType


@dataclass
class PendingClarification:
    """跨轮澄清挂起状态(CLI 层持有,最多 MAX_ROUNDS 轮)。

    轮数上限是交互层的反死循环安全阀(与 Agent.max_turns 同类):意图管线每次产出
    clarification 就挂起一轮,达到上限后由 `terminal/repl.py::_merge_pending` 强制调度
    ——宁可拿不完整的前提跑一次,也不无限追问。上限定义在本类上而不是散落在消费方,
    因为轮数计数器 round 与比较它的阈值必须同源:阈值若在消费方另写一份,改设定时
    两边静默失配(计数器涨而阈值不动,或反之),上限要么形同虚设要么提前触发。

    Attributes:
        question: 向用户提出的澄清问题文本。
        original_input: 产生该澄清的输入（跨轮合并后的文本，包含已收集的澄清上下文）。
            ——超轮终止时以它为最佳猜测调度，比裸原输入更准。
        round: 已询问的澄清轮数，链式累计。
            - 重建时从旧值 +1，绝不重置为 0，否则轮数上限形同虚设。
            - 是否已达上限一律经 `is_exhausted` 判定，不在消费方比较常量。
    """

    #: 最多向用户追问几轮澄清;达到即强制调度(反无限追问的安全阀)。
    #: 收紧/放宽只改这一处——消费方统一经 is_exhausted 判定。
    #: 刻意不做成配置项:这是内部交互安全阀(同 max_turns),不是随环境变化的旋钮。
    MAX_ROUNDS: ClassVar[int] = 2

    question: str
    original_input: str
    round: int = 0

    @property
    def is_exhausted(self) -> bool:
        """已询问轮数是否达到上限（True = 不应再追问，直接强制调度）."""
        return self.round >= self.MAX_ROUNDS


@dataclass
class ConversationState:
    """跨轮会话状态。

    存储上一轮意图和用户输入，供追问检测使用；以及跨轮澄清挂起状态（由 CLI 维护）。
    prev_* 由 agent.py run() 结束后更新;pending_intent 由 CLI 维护。

    Attributes:
        prev_intent: 上一轮识别出的意图类型，用于追问检测（首轮为 None）。
        prev_user_input: 上一轮用户原始输入，用于追问分支重跑实体提取（因不缓存实体，需重提）。
        pending_intent: 跨轮澄清挂起状态，若不为 None 表示当前有未完成的澄清流程。
            - 由 CLI 层维护，最多 `PendingClarification.MAX_ROUNDS` 轮澄清。
    """
    prev_intent: IntentType | None = None                    # 上一轮意图（追问检测消费）
    prev_user_input: str = ""                                # 上一轮输入（追问分支重跑实体提取用）
    pending_intent: PendingClarification | None = None       # 跨轮澄清挂起（CLI 维护，见 MAX_ROUNDS）
