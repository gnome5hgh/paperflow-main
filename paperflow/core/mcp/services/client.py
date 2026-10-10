"""MCP 客户端管理器：自持后台事件循环线程 + 每 server 一条持久会话。

三条实现约束决定了本模块的结构：
- 全部 MCP 会话活在后台循环的常驻 _serve 任务里——anyio cancel scope 要求传输
  上下文的进入与使用同任务，ClientSession 不可跨任务复用；
- 同步 call_tool_sync（runtime 经 asyncio.to_thread 在工作线程调用）用
  concurrent.futures.Future 投递等待——不桥回 REPL 主循环（跨循环会死锁）；
- 会话不可用时重连一次再试（OpenHands v2 同款），仍失败抛 McpToolError，
  调用方（桥接适配器）转为错误 ToolResult 回传模型，绝不抛进 ReAct 循环。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import shutil
import threading
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass

from paperflow.config import McpServerConfig
from paperflow.core.mcp.constants import McpConnectionState
from paperflow.core.mcp.domain.dto.server_status import ServerStatus
from paperflow.core.mcp.domain.dto.tool_spec import McpToolSpec


class McpToolError(Exception):
    """MCP 工具调用失败（未连接/超时/协议错误）；适配器转为错误 ToolResult。"""


@dataclass
class _Request:
    """一条投递给后台循环的调用请求。

    Attributes:
        tool: str，工具名
        arguments: dict，调用参数
        future: asyncio.Future，后台循环上的结果槽（_serve 结算）
    """
    tool: str
    arguments: dict
    future: asyncio.Future         # 后台循环上的 future


def _convert_tool(t) -> McpToolSpec:
    """mcp.types.Tool → McpToolSpec（annotations 转纯 dict，桥接层不碰 SDK 类型）。

    Args:
        t: mcp.types.Tool，SDK 工具对象

    Returns:
        McpToolSpec（annotations 转纯 dict，桥接层不依赖 mcp 包）。
    """
    ann = getattr(t, "annotations", None)
    if ann is not None and hasattr(ann, "model_dump"):
        ann = ann.model_dump(exclude_none=True)
    return McpToolSpec(name=t.name, description=t.description or "",
                       input_schema=getattr(t, "inputSchema", None), annotations=ann)


def result_to_text(result) -> tuple[str, bool]:
    """CallToolResult → (LLM 可读文本, isError)；非文本内容 JSON 化兜底。

    Args:
        result: CallToolResult，MCP 调用结果

    Returns:
        (LLM 可读文本, isError)；无文本内容时兜底 "（空结果）"。
    """
    parts: list[str] = []
    for item in getattr(result, "content", None) or []:
        if getattr(item, "type", "") == "text":
            parts.append(item.text)
        else:
            parts.append(json.dumps(item.model_dump(), ensure_ascii=False, default=str))
    return ("\n".join(parts) or "（空结果）", bool(getattr(result, "isError", False)))


@asynccontextmanager
async def _default_session_factory(cfg: McpServerConfig):
    """默认会话工厂：stdio 子进程 / streamable HTTP；yield 已初始化 ClientSession。

    Args:
        cfg: McpServerConfig，含 transport 与连接参数

    Returns:
        异步上下文管理器，yield 已初始化的 ClientSession（stdio 或 streamable HTTP）。
    """
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from mcp.client.streamable_http import streamablehttp_client

    async with AsyncExitStack() as stack:
        if cfg.transport == "stdio":
            params = StdioServerParameters(command=cfg.command, args=cfg.args,
                                           env=cfg.env or None)
            read, write = await stack.enter_async_context(stdio_client(params))
        else:
            read, write, _sid = await stack.enter_async_context(
                streamablehttp_client(cfg.url, headers=cfg.headers or None))
        session = await stack.enter_async_context(ClientSession(read, write))
        yield session


class McpClientManager:
    """每 enabled server 一条持久会话；未配置/未启动时全部方法安全退化。

    Attributes:
        _servers: dict[str, McpServerConfig]，enabled server 配置
        _session_factory: 会话工厂（缺省 stdio/streamable-http）
        _warn: 告警回调（缺省静默）
        _loop / _thread: 后台事件循环与持有它的守护线程
        _queues / _serve_tasks: dict[str, ...]，每 server 的请求队列与常驻 serve 任务
        _ready: dict[str, concurrent.futures.Future]，首次连接+列表的完成信号
        _reconnect_lock: threading.Lock，重连决策互斥（每 server 至多一条会话）
        _inflight: set，在途的同步调用 future（shutdown 时统一判败唤醒）
        _status: dict[str, ServerStatus]，观测状态的唯一真相源
        _closed: bool，是否已关闭（幂等守卫）
    """

    def __init__(self, servers: dict[str, McpServerConfig],
                 session_factory=None, on_warn=None):
        """记录 server 配置与日志/会话工厂，初始化各状态容器（此时不连任何 server）。

        Args:
            servers: dict[str, McpServerConfig]，server 配置（仅 enabled 者进入 _status）
            session_factory: 会话工厂 | None，缺省用 stdio/streamable-http 实现
            on_warn: 告警回调 | None；显式传入一律生效（含空的自定义收集器），仅 None 静默
        """
        self._servers = dict(servers)
        self._session_factory = session_factory or _default_session_factory
        # 显式传入的 on_warn 一律生效（哪怕对象为假值，如空的自定义收集器），
        # 仅 None 退化为静默——用 `or` 会把假值收集器静默吞掉。
        self._warn = on_warn if on_warn is not None else (lambda msg: None)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queues: dict[str, asyncio.Queue] = {}
        self._serve_tasks: dict[str, asyncio.Task] = {}
        self._ready: dict[str, concurrent.futures.Future] = {}
        self._reconnect_lock = threading.Lock()   # 重连决策互斥：每 server 至多一条会话
        # 在途调用（call_tool_sync 正在阻塞等待的 concurrent.futures.Future）：
        # shutdown 时逐个判败，唤醒卡在工作线程的调用者。
        self._inflight: set[concurrent.futures.Future] = set()
        self._status: dict[str, ServerStatus] = {
            name: ServerStatus(name=name, transport=cfg.transport)
            for name, cfg in self._servers.items() if cfg.enabled}
        self._closed = False

    # —— 生命周期 ——

    def start(self) -> None:
        """拉起后台循环并为每个 enabled server 调度连接+列表（非阻塞预取）。"""
        if self._thread is not None or not self._status:
            return
        self._loop_ready = threading.Event()
        self._thread = threading.Thread(target=self._run_loop, name="mcp-loop", daemon=True)
        self._thread.start()
        self._loop_ready.wait(timeout=5.0)
        for name in list(self._status):
            self._ready[name] = concurrent.futures.Future()
            src = self._servers[name]
            # stdio 命令存在性预检（OpenHands 同款）——不在 PATH 直接失败不
            # spawn，错误信息直接指导用户装 uvx/npx
            if src.transport == "stdio" and shutil.which(src.command) is None:
                self._status[name].status = McpConnectionState.FAILED
                self._status[name].error = f"命令不在 PATH: {src.command}"
                self._ready[name].set_result(False)
                self._warn(f"MCP server '{name}' {self._status[name].error}")
                continue
            self._loop.call_soon_threadsafe(self._spawn_serve, name)

    def _run_loop(self) -> None:
        """后台线程主体：新建事件循环并常驻运行，供全部 MCP 会话与调用使用。"""
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._loop_ready.set()
        loop.run_forever()

    def _spawn_serve(self, name: str) -> None:
        """为某 server 建请求队列并起常驻 serve 任务（须在后台循环上调用）。

        Args:
            name: str，server 名
        """
        self._queues[name] = asyncio.Queue()
        self._serve_tasks[name] = self._loop.create_task(self._serve(name))

    async def _serve(self, name: str) -> None:
        """常驻任务：连接 → 初始化 → 列表 → 循环消费调用请求，直到 shutdown。

        Args:
            name: str，server 名

        Returns:
            无返回值；连接/列表成功即置 ready，随后循环消费请求直到收到停止信号。
        """
        cfg = self._servers[name]
        st = self._status[name]
        try:
            async with self._session_factory(cfg) as session:
                await asyncio.wait_for(session.initialize(), timeout=cfg.connect_timeout)
                listed = await asyncio.wait_for(session.list_tools(),
                                                timeout=cfg.connect_timeout)
                st.tools = [_convert_tool(t) for t in listed.tools]
                st.status = McpConnectionState.CONNECTED
                st.error = ""
                self._ready[name].set_result(True)
                while True:
                    req = await self._queues[name].get()
                    if req is None:
                        return
                    try:                      # 单次调用失败不掀会话
                        res = await asyncio.wait_for(
                            session.call_tool(req.tool, req.arguments),
                            timeout=cfg.call_timeout)
                        req.future.set_result(res)
                    except asyncio.TimeoutError as e:
                        # 我们自己的 wait_for 调用超时：会话仍活着（只是这次调用慢），
                        # 仅本请求判败，状态不动——否则一次慢调用会误杀健康会话。
                        req.future.set_exception(e)
                    except Exception as e:
                        # 真实 SDK 错误模型：工具级错误以 isError=True 的结果返回、
                        # 不抛异常；能从 call_tool 抛出的异常实际只有传输/协议死亡
                        # （uvx 崩溃/OOM/被 kill 等）。此时会话已不可用，标记 failed，
                        # 下一次 call_tool_sync 走既定的"重连一次"路径。
                        st.status = McpConnectionState.FAILED
                        st.error = f"{type(e).__name__}: {e}"
                        req.future.set_exception(e)
        except Exception as e:
            st.status = McpConnectionState.FAILED
            st.error = f"{type(e).__name__}: {e}"
            if name in self._ready and not self._ready[name].done():
                self._ready[name].set_result(False)
                self._warn(f"MCP server '{name}' 连接失败：{st.error}")
            else:
                self._warn(f"MCP server '{name}' 会话中断：{st.error}")
                self._fail_pending(name)

    def _fail_pending(self, name: str) -> None:
        """把队列里尚未执行的请求全部判败（会话中断时调用）。

        Args:
            name: str，server 名
        """
        q = self._queues.get(name)
        while q is not None and not q.empty():
            req = q.get_nowait()
            if req is not None:
                req.future.set_exception(McpToolError(f"server '{name}' 会话中断，请求未执行"))

    def _abort_inflight(self) -> None:
        """把全部在途调用判败。

        McpToolAdapter.execute 经 asyncio.to_thread 在不可取消的工作线程里阻塞
        result(timeout=call_timeout+5)；REPL 退出时 asyncio 要 join 默认执行器，
        若不先唤醒这些线程，退出会冻结到超时。concurrent.futures.Future 的
        set_exception 线程安全；future 已 resolve/cancel 时抛 InvalidStateError /
        CancelledError，吞掉即可（竞态下谁先到谁生效）。
        """
        for fut in list(self._inflight):
            try:
                fut.set_exception(McpToolError("客户端正在关闭，请求中止"))
            except Exception:
                pass
        self._inflight.clear()

    def shutdown(self) -> None:
        """通知各 serve 任务退出并停循环；幂等。"""
        if self._closed:
            return
        self._closed = True
        if self._loop is None:
            return
        self._abort_inflight()

        async def _stop():
            """向各 serve 任务投递停止信号。"""
            for q in self._queues.values():
                await q.put(None)

        try:
            asyncio.run_coroutine_threadsafe(_stop(), self._loop).result(timeout=5.0)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # —— 观测 ——

    def get_server_status(self, name: str) -> ServerStatus | None:
        """取某 server 的观测状态。

        Args:
            name: str，server 名

        Returns:
            ServerStatus；未配置该 server 时 None。
        """
        return self._status.get(name)

    def status_report(self) -> str:
        """/mcp 命令的渲染体：状态 + 工具数 + 被隐藏工具及原因 + 需确认工具。"""
        if not self._servers:
            return "未接入 MCP（config.yaml 顶层 mcp_servers 为空）。"
        lines: list[str] = []
        for name, cfg in self._servers.items():
            if not cfg.enabled:
                lines.append(f"- {name} [{cfg.transport}] 已禁用（enabled: false）")
                continue
            st = self._status[name]
            head = {McpConnectionState.PENDING: "连接中…",
                    McpConnectionState.CONNECTED: "已连接",
                    McpConnectionState.FAILED: f"失败（{st.error}）"}[st.status]
            line = f"- {name} [{cfg.transport}] {head}，工具 {len(st.tools)} 个"
            if st.hidden:
                line += "；已隐藏: " + "、".join(f"{n}（{r}）" for n, r in st.hidden)
            if st.pending_confirm:
                line += f"；需确认: {'、'.join(st.pending_confirm)}（write_tools 可预批准）"
            lines.append(line)
        return "\n".join(lines)

    # —— 调用 ——

    def ensure_ready(self, name: str, timeout: float | None = None) -> bool:
        """阻塞等首次连接+列表；供装配路径与调用前置门。失败/未配置返回 False。

        Args:
            name: str，server 名
            timeout: float | None，等待上限（缺省 connect_timeout + 5s）

        Returns:
            True 表示首次连接+列表已完成；失败/未配置/超时返回 False。
        """
        fut = self._ready.get(name)
        if fut is None:
            return False
        try:
            wait = timeout if timeout is not None else self._servers[name].connect_timeout + 5.0
            return bool(fut.result(timeout=wait))
        except Exception:        # TimeoutError / 未来被取消等一律视为未就绪
            return False

    def _schedule_reconnect(self, name: str) -> None:
        """换新 ready future + 新队列 + 新 serve 任务（重连一次的载体）。

        新 ready future 必须在调用线程同步换上：concurrent.futures.Future 线程安全，
        可跨线程 set_result；若放到循环上的 _do 里异步换新，紧随其后的 ensure_ready
        会读到旧的已完成 future 立即返回，attempt 2 在重连完成前就误判"已重试一次"
        而抛错（真实路径上必现的竞态）。

        Args:
            name: str，server 名
        """
        self._ready[name] = concurrent.futures.Future()

        async def _do():
            """在后台循环上换新队列与 serve 任务，并关闭旧会话/传输。"""
            self._fail_pending(name)                 # 旧队列里未执行的请求直接判败
            old = self._serve_tasks.get(name)
            if old is not None and not old.done():
                old.cancel()                         # 旧 serve 退出 → 工厂 __aexit__
            self._spawn_serve(name)                  # 在同任务内关闭旧会话/传输，
                                                     # 否则旧任务挂在废队列上永久泄漏
        self._loop.call_soon_threadsafe(lambda: self._loop.create_task(_do()))

    def call_tool_sync(self, name: str, tool: str, arguments: dict):
        """同步调用入口（runtime 工作线程）：投递后台循环并等待；不可用重连一次。

        Args:
            name: str，server 名
            tool: str，工具名
            arguments: dict，调用参数

        Returns:
            CallToolResult；不可用/超时/失败抛 McpToolError（调用方转错误 ToolResult）。
        """
        if self._closed or name not in self._status:
            raise McpToolError(f"server '{name}' 不可用（未配置或已关闭）")
        cfg = self._servers[name]
        st = self._status[name]
        for attempt in (1, 2):
            if st.status == McpConnectionState.CONNECTED:
                break
            self.ensure_ready(name)                    # 预取未完成的先等（锁外阻塞）
            if st.status == McpConnectionState.CONNECTED:
                break
            if attempt == 2:
                raise McpToolError(
                    f"server '{name}' 未连接（{st.error or '连接中'}），已重试一次")
            # 临界区只做"是否由本线程调度重连"的决策与调度，绝不含阻塞等待：
            # 并发线程里最多一个真正调度（其余看到在途的未完成 ready future 直接去
            # 等），保证每 enabled server 至多一条 serve 任务/会话。
            with self._reconnect_lock:
                fut = self._ready.get(name)
                if st.status != McpConnectionState.CONNECTED and (fut is None or fut.done()):
                    self._warn(f"MCP server '{name}' 会话不可用，重连一次…")
                    self._schedule_reconnect(name)
            self.ensure_ready(name)                    # 等重连完成（锁外阻塞）
        result_future = concurrent.futures.Future()

        def _submit():
            """在后台循环上把请求投入队列并把结果回填到同步 future。"""
            async def _run():
                """等待队列中该请求的结果或异常，并安全回填同步 future（已判败时忽略）。"""
                fut = self._loop.create_future()
                await self._queues[name].put(_Request(tool, arguments, fut))
                try:
                    res = await fut
                except Exception as e:
                    try:                       # shutdown 已判败（_abort_inflight）时失效即忽略
                        result_future.set_exception(e)
                    except Exception:
                        pass
                else:
                    try:
                        result_future.set_result(res)
                    except Exception:
                        pass
            self._loop.create_task(_run())

        self._loop.call_soon_threadsafe(_submit)
        if self._closed:                       # 与 shutdown 竞态：提交后才关闭则直接判败
            raise McpToolError("客户端正在关闭，请求中止")
        self._inflight.add(result_future)
        try:
            return result_future.result(timeout=cfg.call_timeout + 5.0)
        except concurrent.futures.TimeoutError as e:
            # Python ≥3.11：asyncio.TimeoutError / concurrent.futures.TimeoutError 均为
            # builtin TimeoutError 的别名，_serve 里 wait_for 的调用超时经 future 传回
            # 时才会命中本分支（result 等待上限是 call_timeout+5s，晚于调用超时）。
            raise McpToolError(f"调用超时（>{cfg.call_timeout}s）") from e
        except Exception as e:
            # 工具级失败只判本请求败：状态流转归 _serve（连接/会话级）独占，
            # 这里不动 st——否则一次工具异常会把健康 server 标成 failed，
            # 触发对活会话的无谓重连。
            raise McpToolError(f"调用失败：{e}") from e
        finally:
            self._inflight.discard(result_future)
