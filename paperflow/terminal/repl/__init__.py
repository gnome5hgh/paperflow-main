"""REPL 交互半区：主循环 + 开场呈现 + 历史回放。

`loop.py` 是每轮主循环（读输入 → 驱动 supervisor → 渲染输出），`console.py`
是开场横幅与打印函数工厂，`resume.py` 是 --resume 的屏上历史回放。本包只做
再导出——cli.py 与测试从 `paperflow.terminal.repl` 取。

注意：本包不从 `paperflow.terminal` 包级导出（主循环依赖 core.agent，包级导
出会把重型依赖链拖进 `import paperflow.terminal`）。
"""

from .console import _make_print_fn
from .loop import _repl
from .resume import ResumeReplay, build_resume_replay, render_resume_replay

__all__ = ["_repl", "_make_print_fn", "ResumeReplay", "build_resume_replay",
           "render_resume_replay"]
