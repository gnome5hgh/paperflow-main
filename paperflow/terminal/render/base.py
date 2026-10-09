"""live 块渲染通道的抽象契约。

`BlockRenderer` 定义渲染器可用的四种操作；实现见 blocks.py（RichBlock 走 rich
Live，PlainBlock 走纯文本增量）。渲染器只认契约不认实现，故测试可注入假块。
"""


class BlockRenderer:
    """
    live 块渲染通道的抽象基类。

    定义了四种操作：
        - update(text)：实时更新当前块的内容（重绘）。
        - end(text)：终态渲染并停止块，释放资源。
        - spinner(label)：显示空闲指示（如“working”动画）。
        - show(text)：活动行 + spinner 一体显示（活动流模式 live 区承载物）。

    两种实现：
        - RichBlock：使用 rich.Live 实现 Markdown 富文本重绘（TTY）。
        - PlainBlock：纯文本增量打印（非 TTY 或测试）。
    """

    def update(self, text: str) -> None:
        """实时更新块内容（增量重绘）。

        Args:
            text: str，块的最新完整文本
        """
        raise NotImplementedError

    def end(self, text: str) -> None:
        """终态渲染并收尾（停止 live 或复位状态）。

        Args:
            text: str，块的终态文本
        """
        raise NotImplementedError

    def spinner(self, label: str) -> None:
        """显示空闲工作指示（非 TTY 实现为 no-op）。

        Args:
            label: str，空闲指示的标签文本
        """
        pass

    def show(self, text: str) -> None:
        """活动行 + spinner 一体显示（默认 no-op，与 spinner 同待遇）。

        Args:
            text: str，活动行文本（与 spinner 一体显示）
        """
        pass
