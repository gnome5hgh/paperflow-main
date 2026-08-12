"""终端交互层——REPL 的输入适配与输出渲染，与核心逻辑解耦。

io.py 提供输入适配器（TTY 走 prompt_toolkit，非 TTY 退化为内置 input）；
render.py 提供 rich 渐进式 markdown 流式渲染（替代 cli._ReplStreamer）。
具体导出随各任务逐步加入本文件。
"""
