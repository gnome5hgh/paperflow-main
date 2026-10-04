# paperflow/terminal/render.py
"""输出渲染器——rich 渐进式 markdown 流式渲染 REPL 输出（双模式）。

双模式（终端交互界面重构）：
    - activity=False（非 TTY / 测试）：legacy 路径，行为与重构前完全一致——
      tool 事件（含 tool_start/tool_end，按 "tool" 同型处理）按行打印 ev.text，
      content 增量流式，段切换补换行。
    - activity=True（TTY 装配，见 make_renderer）：ZCode 风格活动流——
      首个 tool_start 之前 root content 照旧 live 流式 markdown；此后所有
      content 静默（不进 live、不进 shown 缓冲），工具调用聚合为活动行
      （RichBlock.show：spinner + 文本一体），动词/agent 键切换或收尾时以
      完成态落屏（✓，慢操作标注耗时）；写类工具的 diffstat 在 finalize 时
      汇成 dim 徽标（`更改 +A -D · K 个文件`）。

should_print 比对「真正渲染过的 root content」（_shown_buffer）与最终答案，
防止重复打印——中间件的 on_finish 钩子可能改写最终回答（如安全扫描的
SAFE_PROMPT 替换）。三态逻辑两种模式共用：直答 shown==result → 只补换行；
有工具轮 shown≠result → 补打最终答案；纯工具轮 shown 空 → 原样打印。

活动行映射（emoji+动词、计数词）与格式化在 paperflow.terminal.activity
（纯函数）；本模块只管 live 区调度、聚合状态机与落屏时机。落屏时机：
动词/agent 键切换、finalize、interrupt、任何 print* 方法前——活动行一旦
落屏不再改动（终端滚动区不可擦除）。

线程安全：on_event 被主 ReAct 的 chat_stream 线程与并行子 agent 的线程池
worker 并发调用（spawn 子 agent 的 tool 事件经上层加前缀透传），_lock 串行化
渲染——同一事件的多段输出整体原子，避免并行子 agent 的工具行交错串字。
should_print / reset / finalize / interrupt 只在主线程调用；suspend 例外——
AskUserQuestionTool.execute 在工具执行器的线程池 worker 里经 ask_user 回调
（terminal.repl 的 _make_ask_callback）也会调它，内部持 _lock 与并发 on_event 串行化，
线程安全。
"""
import threading
import time

from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.spinner import Spinner
from rich.syntax import Syntax
from rich.text import Text

from paperflow.core.agent import StreamEvent
from .activity import activity_label, format_activity
from .diff import truncate_diff


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
        """实时更新块内容（增量重绘）。"""
        raise NotImplementedError

    def end(self, text: str) -> None:
        """终态渲染并收尾（停止 live 或复位状态）。"""
        raise NotImplementedError

    def spinner(self, label: str) -> None:
        """显示空闲工作指示（非 TTY 实现为 no-op）。"""
        pass

    def show(self, text: str) -> None:
        """活动行 + spinner 一体显示（默认 no-op，与 spinner 同待遇）。"""
        pass


class PlainBlock(BlockRenderer):
    """
    纯文本块（非 TTY / 测试）：逐段打印增量，模拟打字机逐字输出。

    特点：
        - content 只追加（append-only）时，仅打印新增部分。
        - 若新文本不是以已显示文本开头（可能被改写），则整段重打（防御性），避免丢字。
        - end 会补打剩余文本并复位 _shown，使下个块从零开始。
    """

    def __init__(self, print_fn):
        """
        Args:
            print_fn: 打印函数，接受 end=/flush= 等 kwargs（如内置 print 或 rich.console.print）。
        """
        self._print = print_fn       # 打印函数（接受 end=/flush= kwargs）
        self._shown = ""             # 已展示的累积文本，用于计算增量

    def update(self, text: str) -> None:
        """流式到达时，仅打印新增部分（增量）。"""
        self._emit_delta(text)

    def end(self, text: str) -> None:
        """终态渲染：补打剩余文本并复位 _shown，供下个块从零开始。"""
        self._emit_delta(text)
        self._shown = ""

    def _emit_delta(self, text: str) -> None:
        """
        计算并输出增量文本。

        若新文本以已显示文本开头（即 append-only 模式），则只打印尾部新增部分；
        否则（文本被改写或重置）整段重打（防御性，避免丢字）。
        """
        if text.startswith(self._shown):
            # 增量打印：只打印新增长度
            self._print(text[len(self._shown):], end="", flush=True)
        else:
            # 非追加场景：整段重打（可能发生在重置或内容替换时）
            self._print(text, end="", flush=True)
        self._shown = text


class StreamRenderer:
    """
    将 Agent 流式事件渲染为终端输出，并决定最终结果如何打印。

    核心职责：
        1. 接收 StreamEvent（content / tool_start / tool_end），按模式渲染。
        2. 使用 BlockRenderer 实现富文本（TTY）或纯文本（非TTY）输出。
        3. 节流重绘（render_interval）避免高频刷新。
        4. 管理 shown 缓冲（仅真正渲染过的 root content）用于 should_print 去重。
        5. 线程安全：所有公共方法持 _lock，防止并发渲染交错。

    双模式（activity 构造参数）：
        - activity=False：legacy 路径。tool 型事件（"tool"/"tool_start"/"tool_end"）
          统一按行打印 ev.text，content 流式进 live 块；root 的工具事件清空
          shown 缓冲（工具前的过程内容不作最终答案缓冲）。
        - activity=True：活动流模型。首个 tool_start 前 content 照旧流式并进
          _shown_buffer；此后 content 全部静默；tool_start 聚合为活动行
          （同动词+同 agent 的连续调用合并计数，live 区 spinner 一体显示）；
          tool_end 记录耗时并累积 diffstat；活动行在键切换/收尾时以完成态落屏。
          ask_user_question 的 tool 事件不出活动行（走确认中心弹框）。

    中断处理：
        - interrupt() 设置 _cancelled 标志，后续 on_event 直接丢弃事件（无法真正取消
          已发出的流）；悬挂的 tool_start（无 end）以完成态收口（duration_ms=None）。
    """

    def __init__(self, print_fn, root_agent_type: str, *, block, render_interval: float = 0.08,
                 console=None, activity: bool = False):
        """
        构造渲染器。

        Args:
            print_fn: 底层打印函数（接受 end/flush/style 等参数）。
            root_agent_type: 根 agent 的类型标识（如 "supervisor"），用于区分 root 和 child content。
            block: BlockRenderer 实例（TTY=RichBlock，非TTY=PlainBlock）。
            render_interval: content 节流重绘间隔（秒），同一间隔内多次 content 只重绘一次。
            console: rich.Console 实例（TTY 下用于 print_diff 语法着色），非 TTY 为 None。
            activity: 活动流模式开关（TTY 装配传 True，见 make_renderer）。
                False 时新增状态全部闲置，行为与 legacy 路径完全一致。
        """
        self._print = print_fn
        self._root = root_agent_type
        self._block = block
        self._render_interval = render_interval
        self._console = console       # TTY rich console（print_diff 着色用）；非 TTY None
        self._activity = activity     # 活动流模式开关（False = legacy 路径）

        # 状态追踪
        self._last_segment = None        # 上一个段类型: None | "root" | "child" | "tool"
        self._shown_buffer: list[str] = []  # 真正渲染过的 root content（should_print 比对用）。
        #   legacy：全部 root content，root 工具事件时清空；activity：仅首个 tool_start
        #   之前的部分，工具事件不清（那些内容确实展示过）。
        self._block_text = ""            # 当前 live 块的累积文本（Markdown 原始内容）
        self._last_render = 0.0          # 上次重绘的时间戳（单调时钟）
        self._cancelled = False          # 中断标志，过滤中断后收到的孤儿事件
        self._current_agent = root_agent_type   # 当前显示的 agent 名称（用于 spinner）
        self._lock = threading.Lock()    # 渲染锁：on_event 跨线程并发调用，锁内串行
        self._suppressed = False         # 确认/提问弹框期间的渲染抑制标志（P0-1）
        self._suppressed_dropped = 0     # 抑制期间被丢弃的事件计数（恢复时提示）

        # 活动流模式状态（activity=False 时全部闲置）
        self._started_tools = False      # 首个 tool_start 后 True；此后 root content 静默
        self._pending: dict | None = None   # 聚合中的活动 {verb, count_word, agent_type,
        #   count, last_summary, duration_ms}；live 区经 block.show 显示
        self._changed: dict[str, tuple[int, int]] = {}   # path → (added, removed)，徽标用

    def reset(self) -> None:
        """
        每轮 run 前调用：清残留状态并重置中断标志。

        清空 shown 缓冲、block_text、活动流状态（_started_tools/_pending/_changed），
        重置 last_segment 和 last_render，取消 cancelled 标记，并启动 spinner 显示
        空闲指示。（异常/澄清路径不消费 should_print，因此每轮必须重置）
        """
        with self._lock:
            self._shown_buffer.clear()
            self._block_text = ""
            self._last_segment = None
            self._last_render = 0.0
            self._cancelled = False
            self._current_agent = self._root
            self._started_tools = False
            self._pending = None
            self._changed = {}
            if self._block is not None:
                self._block.spinner(self._current_agent)   # run 开始 → 空闲指示

    def interrupt(self) -> None:
        """
        Ctrl+C 中断当前 run：设置 cancelled 标志、落屏聚合中的活动行并终止当前 live 块。

        由于 asyncio.to_thread 无法真正取消正在执行的输入线程，
        中断前已发出的后续 StreamEvent 靠此标志丢弃，避免污染界面（已知限制）。
        """
        with self._lock:
            self._cancelled = True
            if self._activity:
                self._commit_pending()
            self._end_block()

    def on_event(self, ev) -> None:
        """
        处理 Agent 流式事件（content/tool_start/tool_end），可被多线程并发调用，锁内串行。

        Args:
            ev: StreamEvent 对象，包含 kind、text、agent_type 及 tool_* 结构化字段。
        """
        with self._lock:
            if self._cancelled:
                return                 # 已中断：丢弃孤儿事件
            if self._suppressed:
                # 确认/提问弹框在前台（P0-1）：任何渲染事件都会重启 Live 把
                # 输入框盖掉（正是并行场景确认框「从不出现」的机制），丢弃并计数
                self._suppressed_dropped += 1
                return
            if self._activity:
                self._on_event_activity(ev)
            else:
                self._on_event_legacy(ev)

    # ── legacy 路径（activity=False，行为与重构前一致）─────────────────

    def _on_event_legacy(self, ev) -> None:
        """legacy 分发：content 流式；tool 型事件（含 tool_start/tool_end）按行打 text。"""
        if ev.kind == "content":
            self._on_content(ev)
        elif ev.kind in ("tool", "tool_start", "tool_end"):
            self._on_tool(ev)

    def _on_content(self, ev) -> None:
        """
        处理 content 事件：累积进当前 live 块，按段切换控制换行。

        段切换规则：
            - root ↔ child：先结束当前块，打印一个换行，再开始新块。
            - tool → content：不补换行（因为 tool 行已显式换行终止）。
            - content → tool：由 _on_tool 处理。
        """
        seg = "root" if ev.agent_type == self._root else "child"
        # 若上一个段是 root 或 child，且与当前段不同，则结束旧块并补换行
        if self._last_segment in ("root", "child") and self._last_segment != seg:
            self._end_block()
            self._print("\n", end="", flush=True)
        # 追加新文本
        self._block_text += ev.text
        self._maybe_render()
        # 仅 root 的流式文本进缓冲，供 should_print 与最终答案比对
        if seg == "root":
            self._shown_buffer.append(ev.text)
        self._last_segment = seg

    def _on_tool(self, ev) -> None:
        """
        处理 tool 型事件（legacy）：打印一行状态行，并在打印前停止 live。

        关键设计：
            - rich.Live 在活动期间，若用 end="" 打印部分行，会被重绘吞掉（中间日志消失）。
              因此必须先 _end_block() 停止 Live，再打印工具行。
            - 工具行以 dim 样式打印，并显式换行。
            - content → tool：先 _end_block() 再补换行（与 content 段分开）。
            - tool → tool：不补换行（避免多余空行）。
            - 工具调用前的 root 流式内容作废（清空 _shown_buffer），
              因为那些内容属于工具执行前的过程，不应作为最终答案的流式缓冲。
        """
        # 如果上一个段是 root/child，需要结束块并补换行
        if self._last_segment in ("root", "child"):
            self._end_block()
            self._print("\n", end="", flush=True)
        else:
            # 上一个段是 tool 或 None，只需结束块（可能残留 spinner）
            self._end_block()
        # 打印工具状态行
        self._print(f"[{ev.agent_type}] {ev.text}", end="", flush=True, style="dim")
        self._print("\n", end="")
        # 若工具是 root agent 调用的，清空 shown 缓冲（之前的中间内容作废）
        if ev.agent_type == self._root:
            self._shown_buffer.clear()
        self._last_segment = "tool"
        self._current_agent = ev.agent_type
        # 恢复 spinner 指示（表示 agent 正在工作）
        if self._block is not None:
            self._block.spinner(self._current_agent)

    # ── 活动流路径（activity=True）──────────────────────────────────

    def _on_event_activity(self, ev) -> None:
        """活动流分发：content → _on_content_activity；tool_start/tool_end → 各自处理。"""
        if ev.kind == "content":
            self._on_content_activity(ev)
        elif ev.kind == "tool_start":
            self._on_tool_start(ev)
        elif ev.kind == "tool_end":
            self._on_tool_end(ev)

    def _on_content_activity(self, ev) -> None:
        """
        处理 content 事件（活动流）。

        - 首个 tool_start 之前：同现状渲染（累积 live 块、节流重绘、root 进 shown 缓冲）。
        - 首个 tool_start 之后：root content 静默丢弃（不进 live、不进缓冲——
          中间过程不该抢滚动区，最终答案由 should_print 交还），child content 同样忽略。
        """
        if self._started_tools:
            return
        self._on_content(ev)

    def _on_tool_start(self, ev) -> None:
        """
        处理 tool_start 事件（活动流）：聚合进当前活动行或开新行。

        - ask_user_question 直接 return（走确认中心弹框，不出活动行）。
        - 否则先 _end_block()（提交已流 prose）→ _commit_pending()（键切换时落屏
          上一活动行）→ _started_tools=True（此后 root content 静默）。
        - 同动词+同 agent 的连续调用合并计数；live 区显示
          format_activity(..., done=False) + "…"（block.show，spinner 一体）。
        """
        if ev.tool_name == "ask_user_question":
            return
        self._end_block()
        verb, count_word = activity_label(ev.tool_name)
        if (self._pending is not None and self._pending["verb"] == verb
                and self._pending["agent_type"] == ev.agent_type):
            # 同键：聚合计数，不落屏（上一活动行仍在进行）
            self._pending["count"] += 1
            self._pending["last_summary"] = ev.summary
        else:
            # 键切换（或首个工具）：先落屏上一活动行，再开新行
            self._commit_pending()
            self._pending = {"verb": verb, "count_word": count_word,
                             "agent_type": ev.agent_type, "count": 1,
                             "last_summary": ev.summary, "duration_ms": None}
        self._started_tools = True
        if self._block is not None:
            p = self._pending
            line = format_activity(p["verb"], p["agent_type"], self._root,
                                   count=p["count"], count_word=p["count_word"],
                                   summary=p["last_summary"])
            self._block.show(line + "…")

    def _on_tool_end(self, ev) -> None:
        """
        处理 tool_end 事件（活动流）：记录耗时、累积 diffstat。

        - ask_user_question 直接 return。
        - pending 同键（动词+agent）→ 记 duration_ms（落屏时 ≥SLOW_MS 才标注）。
        - 键不匹配（或 pending 为 None）→ 先 commit 现有 pending，再直接落屏该
          end 事件的完成行。并行子 agent 交错时 root 的 tool_end 可能晚于子 agent
          开的新行到达——直接丢弃会静默丢掉 root 工具的耗时，故必须落屏。
        - diffstat（仅写类工具）按 path 累积进 _changed，finalize 时汇成徽标。
        """
        if ev.tool_name == "ask_user_question":
            return
        if ev.diffstat is not None:
            path, added, removed = ev.diffstat
            prev_added, prev_removed = self._changed.get(path, (0, 0))
            self._changed[path] = (prev_added + added, prev_removed + removed)
        verb, _ = activity_label(ev.tool_name)
        if (self._pending is not None and self._pending["verb"] == verb
                and self._pending["agent_type"] == ev.agent_type):
            self._pending["duration_ms"] = ev.duration_ms
        else:
            # 键不匹配（或 pending 为 None）：先落屏已有 pending，再直接落屏该事件行
            self._commit_pending()
            self._print(format_activity(verb, ev.agent_type, self._root,
                                        summary=ev.summary,
                                        duration_ms=ev.duration_ms, done=True),
                        end="\n", flush=True, style="dim")

    def _commit_pending(self) -> None:
        """
        落屏聚合中的活动行（完成态），并清空 pending。

        调用时机：动词/agent 键切换、finalize/interrupt、任何 print* 方法前——
        活动行一旦落屏不再改动（终端滚动区不可擦除）。悬挂的 tool_start（无
        tool_end）在此以完成态收口：duration_ms=None（✓ 后无耗时，✓ 表示该活动
        已不再进行）。仅在持 _lock 时调用。
        """
        p = self._pending
        if p is None:
            return
        self._pending = None
        self._print(format_activity(p["verb"], p["agent_type"], self._root,
                                    count=p["count"], count_word=p["count_word"],
                                    summary=p["last_summary"],
                                    duration_ms=p["duration_ms"], done=True),
                    end="\n", flush=True, style="dim")

    # ── 公共终态方法（两模式共用；activity 下先落屏活动行）─────────────

    def _end_block(self) -> None:
        """
        终态渲染当前块并停止 live（或复位 PlainBlock）。

        即使当前块文本为空，也必须调用 end()，因为可能正在显示 spinner。
        若 Live 未启动，RichBlock.end 是幂等（no-op）；PlainBlock.end("") 只补打空增量。
        """
        text, self._block_text = self._block_text, ""
        if self._block:
            self._block.end(text)

    def _maybe_render(self) -> None:
        """
        content 节流重绘：距上次重绘超过 render_interval 才更新 live 块。

        避免高频流式内容导致终端刷新过密。
        """
        if not self._block or not self._block_text:
            return
        now = time.monotonic()
        if now - self._last_render >= self._render_interval:
            self._block.update(self._block_text)
            self._last_render = now

    def finalize(self) -> None:
        """一轮 run 结束（活动流：落屏活动行 → 徽标 → 终态渲染最后一块）。"""
        with self._lock:
            if self._activity:
                self._commit_pending()
                if self._changed:
                    added = sum(a for a, _ in self._changed.values())
                    removed = sum(r for _, r in self._changed.values())
                    self._print(f"更改 +{added} -{removed} · {len(self._changed)} 个文件",
                                end="\n", flush=True, style="dim")
            self._end_block()

    def suppress(self, on: bool) -> int:
        """
        确认中心专用：开启/关闭渲染抑制，返回关闭时被丢弃的事件数。

        弹框前置 True（并终态渲染当前块，与 suspend 等效），弹框结束后置 False。
        线程安全：确认中心消费者与 on_event 可能在不同线程，锁内串行。
        """
        with self._lock:
            self._end_block()
            self._suppressed = on
            if not on:
                dropped = self._suppressed_dropped
                self._suppressed_dropped = 0
                return dropped
            return 0

    def suspend(self) -> None:
        """
        弹输入框/确认框前调用：终态渲染当前块、停 live。

        确保输入提示不会绘制在未完成的 live 块之上，避免混淆。
        该方法可能被工作线程调用（如 AskUserQuestionTool），内部持锁保证线程安全。
        """
        with self._lock:
            self._end_block()

    def print(self, text: str, *, style=None) -> None:
        """
        终态行输出（banner/错误/澄清/run-guard 提示）。

        若当前有 live 块活动，先终态渲染再打印新行；活动流模式下先落屏聚合中的活动行。
        """
        with self._lock:
            if self._activity:
                self._commit_pending()
            self._end_block()
            self._print(text, end="\n", flush=True, style=style)

    def print_diff(self, diff_text: str) -> None:
        """
        打印彩色 unified diff（确认预览用）。

        - TTY 下使用 rich.Syntax 着色，并自动截断超长 diff。
        - 非 TTY 下纯文本直打（无 ANSI）。
        线程安全：确认期间无并发流式事件，但锁内打印确保一致。
        """
        with self._lock:
            if self._activity:
                self._commit_pending()
            self._end_block()
            capped = truncate_diff(diff_text)
            if self._console is not None:
                self._console.print(Syntax(capped, "diff", line_numbers=False), overflow="fold")
            else:
                self._print(capped, end="\n", flush=True)

    def print_markdown(self, text: str, *, style=None) -> None:
        """终态渲染一段 Markdown（会话历史回放用），观感与流式回答落屏后一致。

        为什么不能直接用 print：print 把文本原样交给 console.print，rich 会把其中
        的 ``[x]`` 当标记解析——命中不配对的闭合标签（如正文里的 ``[/note]``）直接抛
        MarkupError，崩掉整轮。Markdown 渲染器不做这层解析，任意历史文本都安全；
        顺带与流式路径渲染回答时的样式完全一致。

        Args:
            text: Markdown 原文；空串直接返回（纯工具调用轮的历史消息 content 为空）。
            style: 保留参数位（非 TTY 路径忽略），供调用方对齐其它终态输出的样式。
        """
        if not text:
            return
        with self._lock:
            if self._activity:
                self._commit_pending()
            self._end_block()
            if self._console is not None:
                self._console.print(Markdown(text), style=style, overflow="fold")
            else:
                self._print(text, end="\n", flush=True)

    def print_raw(self, text: str, *, style=None) -> None:
        """终态行输出，但不解释 rich 标记（会话历史回放的用户文本）。

        用户历史消息是任意文本，含 ``[/x]`` 之类片段时 console.print 会抛
        MarkupError，故 TTY 下先 escape 再打印。非 TTY 路径没有标记语义，必须原样
        输出——escape 会在这里多打出反斜杠。
        """
        with self._lock:
            if self._activity:
                self._commit_pending()
            self._end_block()
            if self._console is not None:
                self._console.print(escape(text), style=style, overflow="fold")
            else:
                self._print(text, end="\n", flush=True)

    def should_print(self, result: str) -> str:
        """
        决定最终答案如何打印，返回「要打印的内容」（空串表示只补换行）。

        逻辑（三态，两模式共用；缓冲为「真正渲染过的 root content」）：
            - 若 _shown_buffer 为空（无流式 content），说明结果未经过流式展示
              （如澄清早退、纯工具轮），需要原样打印 result。
            - 若流式内容与 result 完全相等，说明已逐字展示过，只需补换行（返回空串）。
            - 若流式内容与 result 不相等（被中间件 on_finish 改写，或工具轮后
              内容静默），则补打最终版（前面加换行分隔）。

        Args:
            result: Agent 返回的最终答案字符串。

        Returns:
            应打印的文本（可能为空字符串，此时调用方应 print("") 仅换行）。
        """
        streamed = "".join(self._shown_buffer)
        if not streamed:
            return result               # 没流式（澄清早退/纯工具轮）→ 维持现状
        if streamed == result:
            return ""                   # 已逐字展示 → print("") 只补换行
        return "\n" + result            # 最终回答被改写 → 补打最终版


class RichBlock(BlockRenderer):
    """
    rich Live 区域：把 markdown 缓冲重绘为富文本块（渐进式渲染）。

    update(text) 将文本渲染为 Markdown 并更新 live 区域。
    end(text) 终态渲染并停止 live（若未启动且 text 为空则跳过）。
    spinner(label) 显示带转动动画的指示器。
    show(text) 活动行 + spinner 一体显示（活动流模式 live 区承载物）。

    设计要点：
        - 惰性启动 live（_start()）仅在首次 update/spinner/show 时启动。
        - end() 即使 text 为空也必须停止 live，避免 spinner 残留。
        - live 可注入（测试用），生产时使用共享 Console。
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
        """实时重绘：将 markdown 文本渲染进 live 区域（渐进式展示）。"""
        self._start()
        self._live.update(Markdown(text))

    def end(self, text: str) -> None:
        """终态渲染并停止 live。若未启动且文本为空，则跳过（幂等）。"""
        # 如果已经启动或文本非空（需要渲染），则启动并更新
        if self._started or text:
            self._start()
            self._live.update(Markdown(text))
            self._live.stop()
            self._started = False

    def show(self, text: str) -> None:
        """活动行 + spinner 一体显示（活动流模式 live 区承载物）。"""
        self._start()
        self._live.update(Spinner("dots", text=Text(f" {text}", style="dim"),
                                  style="dim"))

    def spinner(self, label: str) -> None:
        """显示带标签的转动指示器（dim 样式），content update 到达时会被替换。"""
        self.show(f"{label} working")

    def _start(self) -> None:
        """惰性启动 live（refresh=False 避免启动时强制重绘当前帧）。"""
        if not self._started:
            self._live.start(refresh=False)
            self._started = True


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
