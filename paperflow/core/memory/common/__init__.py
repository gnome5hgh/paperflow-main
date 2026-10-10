"""记忆层共用的小件——被多个子包横向消费的领域异常。

`errors.py` 定义 `ConcurrentUpdateError`：写入 CAS 冲突时抛出，供块管理器与
存储层的读-改-写路径共用。它不依赖记忆层的其他子包，故独立成包避免环。

本包只做再导出，消费方从 `paperflow.core.memory.common` 取。
"""

from .errors import ConcurrentUpdateError

__all__ = ["ConcurrentUpdateError"]
