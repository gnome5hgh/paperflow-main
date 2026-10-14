"""工具层的跨模块词汇。

语义根名（`root_hints` 的取值）与几组常用提示根组合——多个工具目录共用同一套
名字，各自写字符串会漂移。本包只做再导出，消费方一律从
`paperflow.tools.common.constants` 取，不关心内部怎么分文件。
"""
from paperflow.tools.common.constants.constants import (
    NOTE_HINTS,
    READ_HINTS,
    SEARCH_HINTS,
)
from paperflow.tools.common.constants.enums import ScopeRoot

__all__ = ["NOTE_HINTS", "READ_HINTS", "SEARCH_HINTS", "ScopeRoot"]
