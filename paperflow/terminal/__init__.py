"""终端交互层——REPL 的输入适配、输出渲染与交互循环，与核心逻辑解耦。

`io/` 提供输入适配器（TTY=prompt_toolkit，非 TTY 内置 input）；`render/` 提供
rich 渐进式 markdown 流式渲染与活动流（非 TTY 退化纯文本）；`confirm/` 是全进程
确认的唯一消费者；`commands/` 是斜杠命令；`repl/` 承载 REPL 主循环与开场呈现；
`common/` 放被多个子包横向消费的小工具（diff、错误翻译）。

包级只统一导出 io/render 的轻量公开接口——repl 依赖 core.agent，包级导出会拖入
重型依赖链，调用方（cli.py / 测试）从 `paperflow.terminal.repl` 直接导入。
"""
from paperflow.terminal.io import InputIO, FallbackIO, PromptToolkitIO, make_input_io
from paperflow.terminal.render import StreamRenderer, PlainBlock, RichBlock, make_renderer

__all__ = [
    "InputIO", "FallbackIO", "PromptToolkitIO", "make_input_io",
    "StreamRenderer", "PlainBlock", "RichBlock", "make_renderer",
]
