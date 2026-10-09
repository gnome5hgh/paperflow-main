"""spawn 派发的跨层契约（单一真相源）。

枚举在 `enums.py`（派发结局）；本包只做再导出，消费方一律从
`paperflow.tools.orchestration.constants` 取。
"""

from .enums import SubAgentStatus

__all__ = ["SubAgentStatus"]
