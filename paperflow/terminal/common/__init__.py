"""终端层共用小工具——被多个子包横向消费的纯函数。

`diff.py` 是写/编辑确认预览的差异计算与截断（render 的 diff 着色、repl 的确认
预览都用它）；`errors.py` 把 API 异常翻译成用户语言（repl 的兜底与 commands 的
handler 都用它）。两者都不依赖终端层的其他子包，故独立成包避免环。

本包只做再导出，消费方从 `paperflow.terminal.common` 取。
"""

from .diff import compute_diff, truncate_diff
from .errors import translate_error

__all__ = ["compute_diff", "truncate_diff", "translate_error"]
