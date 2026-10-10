"""记忆模块的词汇与常量（单一真相源）。

枚举在 `enums.py`（消息角色），工具名集合与播种文案在
`constants.py`；本包只做再导出，消费方一律从 `paperflow.core.memory.constants` 取。
"""

from .constants import (
    BASE_MEMORY_TOOLS,
    DEFAULT_ASSISTANT,
    DEFAULT_PROFILE,
)
from .enums import MessageRole

__all__ = [
    "MessageRole",
    "BASE_MEMORY_TOOLS",
    "DEFAULT_PROFILE",
    "DEFAULT_ASSISTANT",
]
