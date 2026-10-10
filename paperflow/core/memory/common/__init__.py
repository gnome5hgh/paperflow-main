"""记忆层共用的小件——被多个子包横向消费的领域异常。

`errors.py` 定义两个异常：`ConcurrentUpdateError`（写入 CAS 冲突）与
`BlockLimitExceeded`（写入超过块的字符上限，调用方据此换分册）。它们不依赖记忆层
的其他子包，故独立成包避免环。

本包只做再导出，消费方从 `paperflow.core.memory.common` 取。
"""

from .errors import BlockLimitExceeded, ConcurrentUpdateError

__all__ = ["ConcurrentUpdateError", "BlockLimitExceeded"]
