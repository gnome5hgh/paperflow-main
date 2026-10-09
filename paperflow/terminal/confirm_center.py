# paperflow/terminal/confirm_center.py
"""确认中心——全进程确认的唯一消费者。

背景：确认若在 asyncio.to_thread 工作线程里各自跑 prompt_toolkit 临时
prompt，靠线程级锁串行化；并行多 agent 场景下另一 agent 的渲染事件会重启
rich Live 盖掉确认框，持锁线程被 Ctrl+C 卡死后锁永久死锁——确认框永不渲染、
agent 树无限挂起。

设计：所有确认请求跨线程汇入主事件循环上的单一消费者协程，由它独占地
「暂停渲染 → 弹框 → 读输入 → 结算」。要点：

1. **跨事件循环桥接**：子 agent 可能跑在主循环（spawn 改 async 工具后）或
   工作线程的独立循环（遗留路径/测试），统一经 run_coroutine_threadsafe 把
   请求调度到主循环排队，再 wrap_future 回到调用方循环 await——用户按键永远
   回到「活的」await，不再有结果丢弃。
2. **渲染互斥**：消费者弹框期间在渲染器上置抑制标志，其他 agent 的流式事件
   不得重启 Live/打印行把确认框盖掉。
3. **看门狗**：确认请求超过 watchdog_s 无输入 → 自动拒绝，兜底「用户确认等待
   不计入子任务超时」留下的永不超时缺口；方向必须是拒绝（自动同意等于绕过
   安全门）。
4. **三态确认**：y=本次放行 / a=本会话同 (工具,路径) 放行（调用方 pre-confirm
   进 PolicyEngine 已确认集合）/ n=拒绝。
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from paperflow.terminal.activity import activity_label

#: 确认看门狗默认时限（秒）：5 分钟无渲染/无输入即判挂死，自动拒绝
DEFAULT_WATCHDOG_S = 300.0


def _confirm_prompt(cr) -> str:
    """确认框一行提示：左边条 + 图标动词 + 目标 + 键提示。

    图标动词复用活动行的 activity_label；目标取 params 里的 path 尾段
    （basename），取不到 path（如 spawn_sub_agent）就只显示工具名。
    键绑定不变：y=本次放行 / a=本会话放行 / n=拒绝。

    Args:
        cr: ConfirmRequired，待确认的工具调用

    Returns:
        确认框一行提示文本（左边条 + 图标动词 + 目标 + 键提示）。
    """
    tool_name = getattr(cr, "tool_name", "") or ""
    verb, _ = activity_label(tool_name)
    params = getattr(cr, "params", None)
    path = params.get("path") if isinstance(params, dict) else None
    target = (str(path).rstrip("/").rsplit("/", 1)[-1]
              if path else (tool_name or "确认"))
    return f"┃ {verb} {target}　y 放行 / a 本会话放行 / n 拒绝"


@dataclass
class _Request:
    """一次确认请求：payload + 回传 future（绑定主循环）。

    Attributes:
        payload: object，ConfirmRequired
        fut: asyncio.Future，主循环上的回传槽
    """
    payload: object           # ConfirmRequired
    fut: asyncio.Future


class ConfirmCenter:
    """确认的唯一消费者。start() 在主事件循环上启动；confirm() 可从任意
    线程、任意事件循环安全调用。

    Attributes:
        _io: InputIO，终端输入适配
        _renderer: StreamRenderer，渲染通道（弹框期间被子抑制）
        _watchdog_s: float，看门狗时限（秒），超时自动拒绝
        _loop: asyncio.AbstractEventLoop | None，主事件循环（None = 未启动，直连 io 兜底）
        _queue: asyncio.Queue | None，请求队列（唯一消费者逐个处理）
        _task: asyncio.Task | None，常驻消费者协程
    """

    def __init__(self, io, renderer, *, watchdog_s: float = DEFAULT_WATCHDOG_S):
        """记录终端依赖与看门狗时限（此时尚未启动消费者）。

        Args:
            io: InputIO，终端输入适配
            renderer: StreamRenderer，渲染通道
            watchdog_s: float，请求超时自动拒绝的时限（秒）
        """
        self._io = io
        self._renderer = renderer
        self._watchdog_s = watchdog_s
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue | None = None
        self._task: asyncio.Task | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """在当前（主）事件循环上启动消费者协程。_repl 进入循环后调用一次。"""
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        self._task = self._loop.create_task(self._consume())

    async def stop(self) -> None:
        """停止消费者（_repl 退出时）。在途请求按取消收尾，不留任务泄漏。"""
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---------- 调用方入口（任意线程/循环） ----------

    async def confirm(self, cr) -> str:
        """请求三态确认，返回 "y" / "n" / "a"。

        cr: ConfirmRequired（携带工具名与参数，供 diff 预览）。
        可在任意事件循环 await；主循环自身的调用也走队列，与跨线程请求
        同一串行化路径。

        Args:
            cr: ConfirmRequired，待确认的工具调用（携带工具名与参数供 diff 预览）

        Returns:
            "y"（本次放行）/ "a"（本会话同路径放行）/ "n"（拒绝）。
        """
        return await self._bridge(cr)

    async def _bridge(self, cr) -> str:
        """把确认桥接到主循环的消费者；wrap_future 回到调用方循环 await。

        Args:
            cr: ConfirmRequired，待确认的工具调用

        Returns:
            消费者结算的决策；未启动消费者时直连 io 兜底。
        """
        if self._loop is None:
            # 未启动（无 REPL 装配，如测试/程序化调用）：直连 io 兜底
            return self._io.confirm_choice(_confirm_prompt(cr))
        cf = asyncio.run_coroutine_threadsafe(self._serve(cr), self._loop)
        return await asyncio.wrap_future(cf)

    # ---------- 主循环侧 ----------

    async def _serve(self, cr) -> str:
        """（主循环）入队并等待结算。fut 挂在主循环，取消时消费者侧感知。

        Args:
            cr: ConfirmRequired，待确认的工具调用

        Returns:
            该请求的决策字符串（在主循环上入队并等待）。
        """
        fut = self._loop.create_future()
        await self._queue.put(_Request(payload=cr, fut=fut))
        return await fut

    async def _consume(self) -> None:
        """唯一消费者：逐个渲染 + 读输入 + 结算。终端交互全程独占。"""
        while True:
            req = await self._queue.get()
            try:
                if req.fut.done():
                    continue            # 调用方已被取消，无需渲染
                decision = await self._render_and_read_confirm(req.payload)
                if not req.fut.done():
                    req.fut.set_result(decision)
            except asyncio.CancelledError:
                raise
            except Exception:
                # 渲染/读输入异常（如终端异常）：fail-safe 拒绝，不杀消费者
                if not req.fut.done():
                    req.fut.set_result("n")

    async def _render_and_read_confirm(self, cr) -> str:
        """渲染 diff 预览 → 抑制渲染 → 三态读输入（带看门狗）。

        Args:
            cr: ConfirmRequired，待确认的工具调用

        Returns:
            "y"/"a"/"n"；看门狗超时或读输入异常一律返回 "n"（fail-safe 拒绝）。
        """
        from paperflow.terminal.repl import _confirm_diff_preview
        preview = _confirm_diff_preview(cr.tool_name, getattr(cr, "params", None))
        self._renderer.suspend()
        if preview:
            self._renderer.print_diff(preview)
        self._renderer.suppress(True)
        try:
            read = asyncio.ensure_future(asyncio.to_thread(
                self._io.confirm_choice, _confirm_prompt(cr)))
            done, _ = await asyncio.wait({read}, timeout=self._watchdog_s)
            if done:
                return next(iter(done)).result()
            # 看门狗到点：自动拒绝（fail-safe）。读输入线程无法强杀，迟到的
            # 按键在后台被丢弃，终端控制权随即归还。
            self._renderer.print(f"（确认超过 {self._watchdog_s:.0f}s 无响应，已自动拒绝）",
                                 style="yellow")
            return "n"
        except (EOFError, KeyboardInterrupt):
            return "n"
        finally:
            dropped = self._renderer.suppress(False)
            if dropped:
                self._renderer.print(f"（确认期间省略了 {dropped} 条渲染事件）", style="dim")
