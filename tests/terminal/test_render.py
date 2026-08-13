# tests/terminal/test_render.py
import threading

from paperflow.core.agent import StreamEvent
from paperflow.terminal.render import PlainBlock, StreamRenderer


class FakeBlock:
    """渲染通道 fake：记录 update/end 序列，供断言 live 块行为。"""

    def __init__(self):
        self.updates = []
        self.ends = []

    def update(self, text):
        self.updates.append(text)

    def end(self, text):
        self.ends.append(text)

    def spinner(self, label):
        pass


def _collect():
    """返回 (out, print_fn)：print_fn 兼容 end=/flush=/style= kwargs，捕获每次调用首参。"""
    out = []

    def _fn(*a, **k):
        out.append(a[0])
    return out, _fn


def _renderer(fn, **kw):
    """render_interval=0 → 每个 content 事件都渲染，便于确定性断言。"""
    return StreamRenderer(fn, "supervisor", block=PlainBlock(fn), render_interval=0, **kw)


def test_content_segments_insert_newlines_on_transition():
    out, fn = _collect()
    s = _renderer(fn)
    s.on_event(StreamEvent("content", "答", "supervisor"))
    s.on_event(StreamEvent("content", "案", "supervisor"))
    s.on_event(StreamEvent("content", "推理", "searcher"))   # root → child
    s.on_event(StreamEvent("content", "续", "searcher"))
    s.on_event(StreamEvent("content", "总结", "supervisor"))     # child → root
    assert "".join(out) == "答案\n推理续\n总结"


def test_root_tool_event_clears_buffer():
    out, fn = _collect()
    s = _renderer(fn)
    s.on_event(StreamEvent("content", "中间想法", "supervisor"))
    s.on_event(StreamEvent("tool", "调用 search_paper(query=x)", "supervisor"))
    assert s.should_print("最终答案") == "最终答案"    # root 缓冲被清 → 走现状
    s.on_event(StreamEvent("content", "最终答案", "supervisor"))
    assert s.should_print("最终答案") == ""            # 已逐字展示 → 只补换行


def test_should_print_rewrite_case():
    out, fn = _collect()
    s = _renderer(fn)
    s.on_event(StreamEvent("content", "原始内容", "supervisor"))
    assert s.should_print("SAFE_PROMPT") == "\nSAFE_PROMPT"   # on_finish 改写 → 补打


def test_child_content_does_not_pollute_buffer():
    out, fn = _collect()
    s = _renderer(fn)
    s.on_event(StreamEvent("content", "子agent回答", "searcher"))   # child 不入 root 缓冲
    assert s.should_print("最终答案") == "最终答案"
    s.on_event(StreamEvent("content", "最终答案", "supervisor"))
    assert s.should_print("最终答案") == ""


def test_reset_clears_stale_buffer():
    out, fn = _collect()
    s = _renderer(fn)
    s.on_event(StreamEvent("content", "残留", "supervisor"))
    s.reset()
    assert s.should_print("结果") == "结果"           # 残留被清 → 走现状


def test_no_double_newline_with_real_print_behavior():
    """回归：真实 print 默认 end="\\n"，_print("\\n") 必须传 end="" 否则多出空行。"""
    out = []

    def _fn(*a, **k):
        out.append(a[0] + k.get("end", "\n"))
    s = _renderer(_fn)
    s.on_event(StreamEvent("content", "答", "supervisor"))
    s.on_event(StreamEvent("content", "推理", "searcher"))   # root→child 段切换
    s.on_event(StreamEvent("tool", "调用 search_arxiv(query=x)", "searcher"))
    joined = "".join(out)
    assert "\n\n" not in joined
    assert joined == "答\n推理\n[searcher] 调用 search_arxiv(query=x)\n"


def test_no_blank_between_tools_or_tool_to_content():
    """回归 OOS#2：连续工具行、工具行后接内容不得有空行（真实 print 模拟）。"""
    out = []

    def _fn(*a, **k):
        out.append(a[0] + k.get("end", "\n"))
    s = _renderer(_fn)
    s.on_event(StreamEvent("tool", "调用 search_arxiv(query=a)", "supervisor"))
    s.on_event(StreamEvent("tool", "调用 spawn_sub_agent(...)", "supervisor"))
    s.on_event(StreamEvent("content", "最终答案", "supervisor"))
    joined = "".join(out)
    assert "\n\n" not in joined
    assert joined == ("[supervisor] 调用 search_arxiv(query=a)\n"
                      "[supervisor] 调用 spawn_sub_agent(...)\n最终答案")


def test_thread_safe_concurrent_tool_events():
    """线程安全回归：多线程并发调 on_event，无异常、渲染不交错串字。"""
    out, fn = _collect()
    s = _renderer(fn)
    assert isinstance(s._lock, type(threading.Lock()))

    def _hammer(agent_type, rounds):
        # 事件文本不带前缀——前缀由渲染器按 ev.agent_type 统一加
        ev = StreamEvent("tool", "Calling search_arxiv(query=x)", agent_type)
        for _ in range(rounds):
            s.on_event(ev)

    threads = [
        threading.Thread(target=_hammer, args=(f"child{i}", 200))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    tool_lines = [ln for ln in "".join(out).split("\n") if ln]
    assert tool_lines, "并发事件应产生工具行输出"
    for ln in tool_lines:
        assert ln.count("[child") == 1, f"工具行交错：{ln!r}"
    assert s._last_segment == "tool"
    assert s._root_buffer == []


def test_print_ends_active_block_and_emits_line():
    """renderer.print（banner/错误/澄清）在 live 块活动时先终态渲染当前块。"""
    block = FakeBlock()
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=block, render_interval=0)
    s.on_event(StreamEvent("content", "abc", "supervisor"))
    s.print("错误")
    assert block.ends == ["abc"]
    assert out[-1] == "错误"


def test_finalize_ends_last_block():
    block = FakeBlock()
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=block, render_interval=0)
    s.on_event(StreamEvent("content", "答案", "supervisor"))
    s.finalize()
    assert block.ends == ["答案"]


def test_suspend_ends_current_block():
    """suspend（弹输入/确认框前）终态渲染当前块——输入不画在未完块上。"""
    block = FakeBlock()
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=block, render_interval=0)
    s.on_event(StreamEvent("content", "abc", "supervisor"))
    s.suspend()
    assert block.ends == ["abc"]


def test_interrupt_filters_orphan_events():
    """Ctrl+C 中断后，孤儿 to_thread 的后续 StreamEvent 被过滤；reset 后恢复。"""
    block = FakeBlock()
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=block, render_interval=0)
    s.on_event(StreamEvent("content", "abc", "supervisor"))
    s.interrupt()
    s.on_event(StreamEvent("content", "def", "supervisor"))   # 孤儿事件被丢弃
    assert "".join(block.updates) == "abc"
    s.reset()
    s.on_event(StreamEvent("content", "ghi", "supervisor"))
    assert "".join(block.updates) == "abcghi"


def test_throttle_skips_rapid_renders():
    """render_interval=1.0：两次紧邻 content 事件只渲染一次（节流）。"""
    block = FakeBlock()
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=block, render_interval=1.0)
    s.on_event(StreamEvent("content", "a", "supervisor"))
    s.on_event(StreamEvent("content", "b", "supervisor"))   # 1 秒内 → 跳过
    assert len(block.updates) == 1


from unittest.mock import MagicMock

from rich.console import Console
from rich.syntax import Syntax

from paperflow.terminal.render import PlainBlock, RichBlock, make_renderer


def test_rich_block_update_renders_markdown_and_end_stops():
    live = MagicMock()
    block = RichBlock(console=MagicMock(), live=live)
    block.update("# 标题")
    live.start.assert_called_once()
    assert live.update.called                      # 参数是 Markdown 渲染对象
    block.end("正文")
    live.stop.assert_called_once()


def test_make_renderer_tty_uses_rich():
    out, fn = _collect()
    r = make_renderer(fn, "supervisor", is_tty=True, console=Console())
    assert isinstance(r._block, RichBlock)


def test_make_renderer_non_tty_uses_plain():
    out, fn = _collect()
    r = make_renderer(fn, "supervisor", is_tty=False)
    assert isinstance(r._block, PlainBlock)


def test_print_diff_ends_active_block_and_prints_capped_diff():
    """print_diff（确认预览）：先终态渲染当前块；console=None 时纯文本打印截断后的 diff。"""
    block = FakeBlock()
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=block, render_interval=0)
    s.on_event(StreamEvent("content", "abc", "supervisor"))
    diff = "\n".join(f"line{i}" for i in range(300))
    s.print_diff(diff)
    assert block.ends == ["abc"]
    assert len(out[-1].splitlines()) == 201          # 200 行 + 1 行省略标记
    assert out[-1].endswith("… +100 lines")


def test_print_diff_uses_rich_console_when_available():
    """TTY 装配：console 非 None 时 diff 经 rich Syntax 着色输出。"""
    console = MagicMock()
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=PlainBlock(fn), render_interval=0, console=console)
    s.print_diff("-old\n+new")
    console.print.assert_called_once()
    rendered = console.print.call_args[0][0]
    assert isinstance(rendered, Syntax)             # Syntax(diff) 经 rich 着色
    assert rendered.lexer.name == "Diff"


def test_spinner_shown_between_tools_replaced_by_content():
    """空闲段显示 spinner、content 到达被 markdown 替换（FakeBlock 记录 renderable 类型）。"""
    class SpyBlock(FakeBlock):
        def __init__(self):
            super().__init__()
            self.spinners = []
            self.renderables = []      # 每次 update/end/spinner 的类型标记
        def update(self, text): super().update(text); self.renderables.append("content")
        def end(self, text): super().end(text); self.renderables.append("end")
        def spinner(self, label): self.spinners.append(label); self.renderables.append("spinner")

    block = SpyBlock()
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=block, render_interval=0)
    s.reset()                                          # run 开始 → spinner
    assert block.renderables and block.renderables[0] == "spinner"
    assert block.spinners == ["supervisor"]
    s.on_event(StreamEvent("tool", "调用 search_paper(query=x)", "supervisor"))
    s.on_event(StreamEvent("tool", "调用 read_pdf(path=/a.pdf)", "searcher"))   # 子 agent 工具行
    assert block.spinners[-1] == "searcher"            # spinner 显示最近工具行 agent
    s.on_event(StreamEvent("content", "答案", "supervisor"))
    assert block.renderables[-1] == "content"          # content 替换 spinner


def test_spinner_noop_for_plain_block():
    """PlainBlock.spinner 是 no-op（非 TTY 无动画）。"""
    out, fn = _collect()
    s = StreamRenderer(fn, "supervisor", block=PlainBlock(fn), render_interval=0)
    s.reset()                                          # 不抛、无输出变化
    assert "".join(out) == ""


def test_tool_lines_prefixed_with_agent():
    """工具行统一 [{agent}] 前缀（root 也带 [supervisor]，子 agent 带自己名字）。"""
    out, fn = _collect()
    s = _renderer(fn)
    s.on_event(StreamEvent("tool", "Calling spawn_sub_agent(...)", "supervisor"))
    s.on_event(StreamEvent("tool", "Calling read_file(path=/a.md)", "writer"))
    joined = "".join(out)
    assert "[supervisor] Calling spawn_sub_agent(...)" in joined
    assert "[writer] Calling read_file(path=/a.md)" in joined
