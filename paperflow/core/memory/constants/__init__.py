"""记忆模块的词汇与常量。

工具名集合与播种文案在 `constants.py`；本包只做再导出，消费方一律从
`paperflow.core.memory.constants` 取。消息角色 `MessageRole` 被 wire 消息家族与
本模块的持久化消息共同消费，已归 `paperflow.core.common.constants`。
"""

from .constants import (
    BASE_MEMORY_TOOLS,
    DEFAULT_ASSISTANT,
    DEFAULT_PROFILE,
)

__all__ = [
    "BASE_MEMORY_TOOLS",
    "DEFAULT_PROFILE",
    "DEFAULT_ASSISTANT",
]
