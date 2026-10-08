"""引用域的跨模块词汇。

这些值经工具输出与 summary 回给 LLM，改值等于改工具输出契约。枚举在
`enums.py`；本包只做再导出，消费方一律从 `paperflow.citations.constants` 取。
"""

from .enums import CitationStatus, RemoveOutcome

__all__ = ["CitationStatus", "RemoveOutcome"]
