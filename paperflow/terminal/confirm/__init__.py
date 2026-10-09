"""确认域：确认中心 + Agent 侧确认回调 + 确认框呈现。

`center.py` 是全进程确认的唯一消费者（消费者协程 + 跨事件循环桥接 + 看门狗），
并附带把三态选择折叠成放行/拒绝的 Agent 侧回调；`presentation.py` 只产出确认框
与 diff 预览的文本。诊断与排查见 center.py 的模块文档串。

本包只做再导出，消费方从 `paperflow.terminal.confirm` 取。
"""

from .center import ConfirmCenter, _make_confirm_callback
from .presentation import _confirm_diff_preview, _confirm_prompt

__all__ = ["ConfirmCenter", "_make_confirm_callback",
           "_confirm_diff_preview", "_confirm_prompt"]
