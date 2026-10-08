"""由模式枚举派生的白名单。"""

from .enums import SubAgentMode

__all__ = ["SUB_AGENT_MODES"]

#: 合法 mode 值集合——spawn 运行时校验兜底（schema enum 约束 LLM 生成层，
#: 此集合兜住任何漏网之鱼，防拼写错静默错流）。
SUB_AGENT_MODES = frozenset(m.value for m in SubAgentMode)
