"""工具层共享常量。

`root_hints` 的取值必须与 `_root_map` 的键一致，否则 `[目录]` 提示行静默消失；
下面几组是多个工具共用的组合，收在这里免得各自维护导致漂移。
"""

from paperflow.tools.common.constants.enums import ScopeRoot

__all__ = [
    "NOTE_HINTS",
    "READ_HINTS",
    "SEARCH_HINTS",
    "ScopeRoot",
]

#: 笔记类工具（write/edit）的提示根：产笔记、查记忆、写选题产物
NOTE_HINTS = [ScopeRoot.NOTE, ScopeRoot.MEMORY, ScopeRoot.RESEARCH]

#: 检索类只读工具（glob/grep）的提示根：在笔记与论文里找东西
SEARCH_HINTS = [ScopeRoot.NOTE, ScopeRoot.PDF, ScopeRoot.MEMORY]

#: 全文读工具的提示根：多一个 scratch（草稿也要能读回）
READ_HINTS = [
    ScopeRoot.NOTE,
    ScopeRoot.PDF,
    ScopeRoot.MEMORY,
    ScopeRoot.SCRATCH,
    ScopeRoot.RESEARCH,
]
