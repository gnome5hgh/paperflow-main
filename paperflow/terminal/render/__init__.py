"""输出渲染：契约、两种块实现、活动流渲染器与装配工厂。

`base.py` 是契约，`blocks.py` 是两种实现，`renderer.py` 承载活动流状态机，
`activity.py` 是工具事件 → 活动行的词汇表（活动流与确认框共用）。本包只做再
导出与装配——消费方从 `paperflow.terminal.render` 取。
"""

from .base import BlockRenderer
from .blocks import PlainBlock, RichBlock
from .renderer import StreamRenderer


def make_renderer(print_fn, root_agent_type: str, *, is_tty: bool, console=None) -> StreamRenderer:
    """
    工厂函数：根据终端类型装配合适的渲染器。

    Args:
        print_fn: 底层打印函数。
        root_agent_type: 根 agent 类型标识。
        is_tty: 是否为交互式终端。
        console: TTY 下的 rich.Console 实例（用于 print_diff 着色）。

    Returns:
        StreamRenderer: 配置好的渲染器实例。
            若 is_tty=True，使用 RichBlock（富文本 Markdown）+ 活动流模式。
            否则使用 PlainBlock（纯文本增量打印）+ legacy 路径。
    """
    if is_tty:
        return StreamRenderer(print_fn, root_agent_type,
                              block=RichBlock(console=console), console=console,
                              activity=True)
    return StreamRenderer(print_fn, root_agent_type, block=PlainBlock(print_fn))


__all__ = ["StreamRenderer", "BlockRenderer", "PlainBlock", "RichBlock", "make_renderer"]
