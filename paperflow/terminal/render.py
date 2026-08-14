"""输出渲染器——rich 渐进式 markdown 流式渲染 REPL 输出。

段模型：root/child content（live 重绘的 markdown 块）+ tool（状态行）。
content 累积成 markdown 缓冲、节流重绘 live 块；tool 行终态打印并清 root
缓冲。should_print 比对流式缓冲与最终答案，防止最终答案重复打印——中间件的
on_finish 钩子可能改写最终回答（如安全扫描的 SAFE_PROMPT 替换）。

线程安全：on_event 被主 ReAct 的 chat_stream 线程与并行子 agent 的线程池
worker 并发调用（spawn 子 agent 的 tool 事件经上层加前缀透传），_lock 串行化
渲染——同一事件的多段输出整体原子，避免并行子 agent 的工具行交错串字。
should_print / reset / finalize / suspend / interrupt 只在主线程调用。
"""
import threading
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text

from paperflow.core.agent import StreamEvent
from .diff import truncate_diff


class BlockRenderer:
    """live 块渲染通道。update(text)=实时重绘；end(text)=终态渲染并收尾；spinner(label)=空闲指示。"""

    def update(self, text: str) -> None:
        raise NotImplementedError

    def end(self, text: str) -> None:
        raise NotImplementedError

    def spinner(self, label: str) -> None:
        """空闲段工作指示（非 TTY 实现为 no-op）。"""
        pass


class PlainBlock(BlockRenderer):
    """纯文本块（非 TTY / 测试）：逐段打印增量，模拟打字机逐字输出。

    content 只追加（append-only），update 打印自上次展示以来的新增文本；end 补打
    剩余并复位，供下个块从零开始。_emit_delta 对 append-only 走增量、否则整段
    重打（防御性），避免重复或丢字。
    """

    def __init__(self, print_fn):
        self._print = print_fn       # 打印函数（接受 end=/flush= kwargs）
        self._shown = ""             # 已展示的累积文本，用于算增量

    def update(self, text: str) -> None:
        """流式到达：只打印新增部分（增量）。"""
        self._emit_delta(text)

    def end(self, text: str) -> None:
        """终态渲染：补打剩余文本并复位，供下个块从零开始。"""
        self._emit_delta(text)
        self._shown = ""

    def _emit_delta(self, text: str) -> None:
        # 文本只追加时增量打印尾部；被改写（非追加）时整段重打（防御，避免丢字）
        if text.startswith(self._shown):
            self._print(text[len(self._shown):], end="", flush=True)
        else:
            self._print(text, end="", flush=True)
        self._shown = text


class StreamRenderer:
    """把 Agent 流式事件渲染为终端输出，并决定最终结果如何打印。"""

    def __init__(self, print_fn, root_agent_type: str, *, block, render_interval: float = 0.08,
                 console=None):
        """构造渲染器。block 是渲染通道（TTY=RichBlock / 非 TTY=PlainBlock）。

        render_interval 是 content 节流间隔（秒）：同一间隔内的连续 content 只重绘
        一次，避免高频流式刷屏。console 仅在 TTY 下非 None，供 print_diff 语法着色。
        """
        self._print = print_fn
        self._root = root_agent_type
        self._block = block
        self._render_interval = render_interval
        self._console = console       # TTY rich console（print_diff 着色用）；非 TTY None
        self._last_segment = None        # None | "root" | "child" | "tool"
        self._root_buffer: list[str] = []   # 仅 root content，用于 should_print 比对
        self._block_text = ""            # 当前 live 块的流式文本
        self._last_render = 0.0
        self._cancelled = False
        self._current_agent = root_agent_type   # spinner 显示的 agent（最近工具行 agent 或 root）
        self._lock = threading.Lock()    # 渲染锁：on_event 跨线程并发调用，锁内串行

    def reset(self) -> None:
        """每轮 run 前调用：清残留 + 清「已中断」标志（异常/澄清路径不消费 should_print）。"""
        with self._lock:
            self._root_buffer.clear()
            self._block_text = ""
            self._last_segment = None
            self._last_render = 0.0
            self._cancelled = False
            self._current_agent = self._root
            if self._block is not None:
                self._block.spinner(self._current_agent)   # run 开始 → 空闲指示

    def interrupt(self) -> None:
        """Ctrl+C 中断当前 run：置「已中断」标志过滤孤儿事件。

        输入线程经 to_thread 无法真正取消，其中断前已发出的后续 StreamEvent 靠此
        标志丢弃，避免污染界面（已知限制）。
        """
        with self._lock:
            self._cancelled = True
            self._end_block()

    def on_event(self, ev) -> None:
        """处理 Agent 流式事件（content/tool）。可被多线程并发调用，锁内串行。"""
        with self._lock:
            if self._cancelled:
                return                 # 已中断：丢弃孤儿事件
            if ev.kind == "content":
                self._on_content(ev)
            elif ev.kind == "tool":
                self._on_tool(ev)

    def _on_content(self, ev) -> None:
        """content 事件：累积进当前 live 块，按段切换控制换行。"""
        seg = "root" if ev.agent_type == self._root else "child"
        # 段切换（root↔child）才补换行；tool→content 不补（工具行已显式 \n 终止）
        if self._last_segment in ("root", "child") and self._last_segment != seg:
            self._end_block()
            self._print("\n", end="", flush=True)
        self._block_text += ev.text
        self._maybe_render()
        if seg == "root":
            # 仅 root 的流式文本进缓冲，供 should_print 与最终答案比对
            self._root_buffer.append(ev.text)
        self._last_segment = seg

    def _on_tool(self, ev) -> None:
        """tool 事件：打印一行「[agent] 工具调用」状态行，并在打印前停 live。

        工具行必须在无 live 活动下打印：rich Live 对用 end="" 打印的部分行会重绘吞掉
        （中间日志会消失、只剩空行与 spinner）。故无论上一段是 content 还是 tool，
        打印前先 _end_block() 停 live——spinner 经 end("") 清行、content 块经终态
        渲染。content→tool 补换行；tool→tool 不补（避免空行）。
        """
        if self._last_segment in ("root", "child"):
            self._end_block()
            self._print("\n", end="", flush=True)
        else:
            self._end_block()
        self._print(f"[{ev.agent_type}] {ev.text}", end="", flush=True, style="dim")
        self._print("\n", end="")
        if ev.agent_type == self._root:
            self._root_buffer.clear()   # 工具调用前的中间内容作废，只留最终轮的流式文本
        self._last_segment = "tool"
        self._current_agent = ev.agent_type
        if self._block is not None:
            self._block.spinner(self._current_agent)   # 工具行打印后恢复空闲指示

    def _end_block(self) -> None:
        """终态渲染当前块并停 live。空文本（纯工具轮/澄清轮）也必须调 end——
        否则 Live 残留 spinner 动画，会在确认 diff 打印与下个输入提示下继续转。
        对未启动的 live 调用 end 是安全的：RichBlock.end 对未启动是 no-op、
        PlainBlock.end("") 只补打空增量。
        """
        text, self._block_text = self._block_text, ""
        if self._block:
            self._block.end(text)

    def _maybe_render(self) -> None:
        """content 节流重绘：距上次重绘超过间隔才更新 live 块。"""
        if not self._block or not self._block_text:
            return
        now = time.monotonic()
        if now - self._last_render >= self._render_interval:
            self._block.update(self._block_text)
            self._last_render = now

    def finalize(self) -> None:
        """一轮 run 结束：终态渲染最后一个块（后续 should_print 决定是否补打）。"""
        with self._lock:
            self._end_block()

    def suspend(self) -> None:
        """弹输入框/确认框前调用：终态渲染当前块、停 live（输入不画在未完块上）。"""
        with self._lock:
            self._end_block()

    def print(self, text: str, *, style=None) -> None:
        """终态行输出（banner/错误/澄清/run-guard 提示）。live 块活动时先终态渲染。"""
        with self._lock:
            self._end_block()
            self._print(text, end="\n", flush=True, style=style)

    def print_diff(self, diff_text: str) -> None:
        """打印彩色 unified diff（确认预览用）。TTY 经 rich Syntax(diff) 着色、超长截断；
        非 TTY 纯文本直打（无 ANSI）。线程安全：确认期间无并发流式事件，锁内打印。"""
        with self._lock:
            self._end_block()
            capped = truncate_diff(diff_text)
            if self._console is not None:
                self._console.print(Syntax(capped, "diff", line_numbers=False), overflow="fold")
            else:
                self._print(capped, end="\n", flush=True)

    def should_print(self, result: str) -> str:
        """决定最终答案如何打印，返回「要打印的内容」（空串 = 只补换行）。

        用 root 流式缓冲与 Agent 返回的最终 result 比对：未流式（澄清早退/纯工具轮）
        原样打印；已逐字展示过只补换行；中间件 on_finish 改写了最终回答（如安全扫描
        的 SAFE_PROMPT 替换）则补打最终版。
        """
        streamed = "".join(self._root_buffer)
        if not streamed:
            return result               # 没流式（澄清早退/纯工具轮）→ 维持现状
        if streamed == result:
            return ""                   # 已逐字展示 → print("") 只补换行
        return "\n" + result            # 最终回答被改写 → 补打最终版


class RichBlock(BlockRenderer):
    """rich Live 区域：把 markdown 缓冲重绘为富文本块（渐进式渲染）。

    update 重绘 live 区域（每片 Markdown 重渲染）；end 终态渲染并停止 live。
    live 可注入（测试传 fake 断言 start/update/stop 序列）；生产由 make_renderer
    用共享 Console 构造——同一 console 的样式（工具行 dim）与 live 区域不打架。
    """

    def __init__(self, console=None, live=None):
        """构造 rich Live 块。console/live 可注入（测试传 fake 断言调用序列）。

        生产由 make_renderer 传共享 Console——同一 console 的样式（工具行 dim）
        与 live 区域不打架。refresh_per_second 是 rich 重绘帧率上限。
        """
        self._console = console or Console()
        self._live = live or Live(console=self._console, refresh_per_second=20)
        self._started = False

    def update(self, text: str) -> None:
        """实时重绘：把 markdown 文本渲染进 live 区域（渐进式展示）。"""
        self._start()
        self._live.update(Markdown(text))

    def end(self, text: str) -> None:
        """终态渲染并停 live。未启动过且文本为空时跳过（幂等，Live 不残留）。"""
        if self._started or text:
            self._start()
            self._live.update(Markdown(text))
            self._live.stop()
            self._started = False

    def spinner(self, label: str) -> None:
        """Live 显示 dim spinner「label working」，content update 到达即被替换。"""
        self._start()
        self._live.update(Spinner("dots", text=Text(f" {label} working", style="dim"),
                                  style="dim"))

    def _start(self) -> None:
        """惰性启动 live（refresh=False：启动时不强制重绘当前帧）。"""
        if not self._started:
            self._live.start(refresh=False)
            self._started = True


def make_renderer(print_fn, root_agent_type: str, *, is_tty: bool, console=None) -> StreamRenderer:
    """装配渲染器：is_tty → RichBlock（rich 渐进 markdown）；否则 PlainBlock（纯文本）。"""
    if is_tty:
        return StreamRenderer(print_fn, root_agent_type,
                              block=RichBlock(console=console), console=console)
    return StreamRenderer(print_fn, root_agent_type, block=PlainBlock(print_fn))
