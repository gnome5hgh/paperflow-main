"""两种块渲染实现：rich Live 富文本（TTY）与纯文本增量（非 TTY）。

两者都实现 `BlockRenderer` 契约，由 make_renderer 按终端类型二选一；行为对齐的
关键是 `end()` 幂等收尾（PlainBlock 复位累积缓冲，RichBlock 停 live），否则
spinner 会残留或下个块丢字。
"""
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.text import Text

from .base import BlockRenderer


class PlainBlock(BlockRenderer):
    """纯文本块（非 TTY / 测试）：逐段打印增量，模拟打字机逐字输出。

    特点：
        - content 只追加（append-only）时，仅打印新增部分。
        - 若新文本不是以已显示文本开头（可能被改写），则整段重打（防御性），避免丢字。
        - end 会补打剩余文本并复位 _shown，使下个块从零开始。

    Attributes:
        _print: 回调，打印函数（接受 end=/flush= 等 kwargs）
        _shown: str，已展示的累积文本（增量比对基准）
    """

    def __init__(self, print_fn):
        """
        Args:
            print_fn: 打印函数，接受 end=/flush= 等 kwargs（如内置 print 或 rich.console.print）。
        """
        self._print = print_fn       # 打印函数（接受 end=/flush= kwargs）
        self._shown = ""             # 已展示的累积文本，用于计算增量

    def update(self, text: str) -> None:
        """流式到达时，仅打印新增部分（增量）。

        Args:
            text: str，块的最新完整文本（只打印增量部分）
        """
        self._emit_delta(text)

    def end(self, text: str) -> None:
        """终态渲染：补打剩余文本并复位 _shown，供下个块从零开始。

        Args:
            text: str，块的终态文本（补打剩余并复位 _shown）
        """
        self._emit_delta(text)
        self._shown = ""

    def _emit_delta(self, text: str) -> None:
        """计算并输出增量文本。

        若新文本以已显示文本开头（即 append-only 模式），则只打印尾部新增部分；
        否则（文本被改写或重置）整段重打（防御性，避免丢字）。

        Args:
            text: str，块的最新完整文本

        Returns:
            无返回值；append-only 时只打印尾部新增，文本被改写时整段重打（防御性）。
        """
        if text.startswith(self._shown):
            # 增量打印：只打印新增长度
            self._print(text[len(self._shown):], end="", flush=True)
        else:
            # 非追加场景：整段重打（可能发生在重置或内容替换时）
            self._print(text, end="", flush=True)
        self._shown = text


class RichBlock(BlockRenderer):
    """rich Live 区域：把 markdown 缓冲重绘为富文本块（渐进式渲染）。

    update(text) 将文本渲染为 Markdown 并更新 live 区域。
    end(text) 终态渲染并停止 live（若未启动且 text 为空则跳过）。
    spinner(label) 显示带转动动画的指示器。
    show(text) 活动行 + spinner 一体显示（活动流模式 live 区承载物）。

    设计要点：
        - 惰性启动 live（_start()）仅在首次 update/spinner/show 时启动。
        - end() 即使 text 为空也必须停止 live，避免 spinner 残留。
        - live 可注入（测试用），生产时使用共享 Console。

    Attributes:
        _console: rich.Console，控制台实例
        _live: rich.Live，Live 区域（惰性启动）
        _started: bool，live 是否已启动
    """

    def __init__(self, console=None, live=None):
        """
        构造 rich Live 块。

        Args:
            console: rich.Console 实例（可共享，确保工具行 dim 样式与 live 区域不冲突）。
            live: 可注入的 Live 实例（测试用），默认使用 20fps 刷新率。
        """
        self._console = console or Console()
        self._live = live or Live(console=self._console, refresh_per_second=20)
        self._started = False

    def update(self, text: str) -> None:
        """实时重绘：将 markdown 文本渲染进 live 区域（渐进式展示）。

        Args:
            text: str，块的 Markdown 文本（实时重绘进 live 区域）
        """
        self._start()
        self._live.update(Markdown(text))

    def end(self, text: str) -> None:
        """终态渲染并停止 live。若未启动且文本为空，则跳过（幂等）。

        Args:
            text: str，终态文本；未启动且为空则跳过（幂等）
        """
        # 如果已经启动或文本非空（需要渲染），则启动并更新
        if self._started or text:
            self._start()
            self._live.update(Markdown(text))
            self._live.stop()
            self._started = False

    def show(self, text: str) -> None:
        """活动行 + spinner 一体显示（活动流模式 live 区承载物）。

        Args:
            text: str，活动行文本（活动流 live 区的承载物）
        """
        self._start()
        self._live.update(Spinner("dots", text=Text(f" {text}", style="dim"),
                                  style="dim"))

    def spinner(self, label: str) -> None:
        """显示带标签的转动指示器（dim 样式），content update 到达时会被替换。

        Args:
            label: str，指示器标签文本
        """
        self.show(f"{label} working")

    def _start(self) -> None:
        """惰性启动 live（refresh=False 避免启动时强制重绘当前帧）。"""
        if not self._started:
            self._live.start(refresh=False)
            self._started = True
