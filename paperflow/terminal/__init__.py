"""终端交互层——REPL 的输入适配与输出渲染，与核心逻辑解耦。

io.py 提供输入适配器（TTY 走 prompt_toolkit，非 TTY 退化为内置 input）；
render.py 提供 rich 渐进式 markdown 流式渲染（替代 cli._ReplStreamer）。
包级统一导出公开接口：调用方（cli.py / 测试）从 paperflow.terminal 直接导入，
不必深入子模块。
"""
from paperflow.terminal.io import InputIO, FallbackIO, PromptToolkitIO, make_input_io
from paperflow.terminal.render import StreamRenderer, PlainBlock, RichBlock, make_renderer

__all__ = [
    "InputIO", "FallbackIO", "PromptToolkitIO", "make_input_io",
    "StreamRenderer", "PlainBlock", "RichBlock", "make_renderer",
]
