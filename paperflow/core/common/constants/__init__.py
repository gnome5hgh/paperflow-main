"""core 内跨子包共享的常量（单一真相源）。

枚举在 `enums/`（一枚举一文件），其余常量在 `constants.py`；本包只做再导出，
消费方一律从 `paperflow.core.common.constants` 取。
"""
from paperflow.core.common.constants.constants import RETRY_BACKOFF_BASE
from paperflow.core.common.constants.enums import MessageRole

__all__ = ["MessageRole", "RETRY_BACKOFF_BASE"]
