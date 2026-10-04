"""跨轮会话状态容器:意图追问上下文(CLI 持有、传给 Supervisor)。

意图管线的追问检测消费 prev_intent / prev_user_input(上一轮意图与输入)。跨轮上下文
由上下文压缩器的历史累积承担(CLI 复用同一 Supervisor 实例,压缩器常驻),本状态不
冗余保存摘要。

澄清不走跨轮状态（2026-10-04 澄清统一）：管线判定该问时由 runtime 在本轮内直接调
ask 回调同步问用户（routing.confirm 原语解析、意图代码级落地），不再挂起到下一轮
——旧机制（PendingClarification / _merge_pending / MAX_ROUNDS / force_dispatch）
随之退役。agent 执行中途问用户走 ask_user_question 工具（intent_options 参数），
同一原语、同一落地代码。
"""
from dataclasses import dataclass

from paperflow.core.intent.schemas.intent import IntentType


@dataclass
class ConversationState:
    """跨轮会话状态。

    prev_* 由 agent.run() 结束后更新。

    Attributes:
        prev_intent: 上一轮识别出的意图类型，用于追问检测（首轮为 None）。
        prev_user_input: 上一轮用户原始输入，用于追问分支重跑实体提取（因不缓存实体，需重提）。
    """
    prev_intent: IntentType | None = None                    # 上一轮意图（追问检测消费）
    prev_user_input: str = ""                                # 上一轮输入（追问分支重跑实体提取用）
