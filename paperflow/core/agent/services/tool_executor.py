"""工具调用执行管道：中间件洋葱 + 参数解析 + 三把锁 + 结构化事件收口。

从 Agent 上整块搬出来的——它此前与 Agent 实例强耦合（读 7 个 self 属性），
现在是一个独立的协作对象。**只有构造后不会被替换的东西才按值注入**：

- 构造期：中间件链、agent_type / session_id / instance_id / has_human_confirm；
- 调用期传入：`tools`、`confirm_callback`、`stream_sink` —— 这三样在真实流程里会被构造后
  替换（spawn 用 `_wrap_confirm_callback` 把用户确认等待排除出超时预算；REPL 在
  renderer 造好之后才把流式回调挂到 supervisor；测试直接整表换 `tools`）。按值
  捕获会拿到过期引用，所以每次调用由调用方现取现传。

管道主体是「查工具 → 解析参数 → 写类采样旧文本 → 三把锁 → before 钩子 → 执行 →
逆序 after 钩子 → 一条 tool_end 收口」，也是「所有错误降级为文本」这条策略的唯一
落点：任何异常都变成 ToolResult 文本回给模型（只有 MaxTurnsExceeded 向上抛，
那在 Agent.run 里）。
"""

import asyncio
import difflib
import json
import sys
import time
from datetime import datetime
from pathlib import Path

from paperflow.core.agent.constants import AgentEventKind
from paperflow.core.agent.domain import AgentEvent
from paperflow.core.agent.services.state import get_run_state
from paperflow.core.security import (
    ApprovalOutcome, ConfirmRequired, SecurityError, ToolContext,
)
from paperflow.core.tool import (
    SUMMARY_DECISION, SUMMARY_VIOLATIONS, ToolResult, is_writer,
)

__all__ = ["ToolExecutor"]


_PATH_LOCKS: dict[str, asyncio.Lock] = {}


def _path_lock(path: str) -> asyncio.Lock:
    """取目标路径的串行锁（无则建；setdefault 原子，多循环场景安全）。

    Args:
        path: str，目标文件路径

    Returns:
        该路径的 asyncio.Lock（无则建；同一文件的并发调用拿到同一把锁）。
    """
    # 键 = 目标文件路径
    # setdefault 原子地“无则建、有则取”，保证同一文件的所有并发调用拿到的是同一把锁。
    return _PATH_LOCKS.setdefault(path, asyncio.Lock())


def _diffstat(old: str, new: str) -> tuple[int, int]:
    """unified diff 行级统计 (added, removed)，不计 +++/--- 头。

    Args:
        old: str，旧的完整文本
        new: str，新的完整文本

    Returns:
        (added, removed) 行级统计（不计 +++/--- 头）。
    """
    added = removed = 0
    for line in difflib.unified_diff(old.splitlines(), new.splitlines(), n=0):
        if line.startswith("+") and not line.startswith("+++"):
            added += 1
        elif line.startswith("-") and not line.startswith("---"):
            removed += 1
    return added, removed


def _read_text_or_none(path: str) -> str | None:
    """读文件文本；不存在返回 ""（新写文件场景），IO/解码失败返回 None（无徽标）。

    Args:
        path: str，文件路径

    Returns:
        文件文本；不存在返回 ""（新写文件场景），IO/解码失败返回 None（不发徽标）。
    """
    try:
        p = Path(path)
        return p.read_text(encoding="utf-8") if p.exists() else ""
    except (OSError, UnicodeDecodeError):
        return None



class ToolExecutor:
    """单个工具调用的执行管道（无状态；可变依赖由调用方每次传入）。

    Attributes:
        _middleware: list[SecurityMiddleware]，before 顺序 / after 逆序
        _agent_type / _session_id: str，写进 ToolContext 供审计聚合
        _instance_id: str，本实例标识（同路径写占用的持有者判定）
        _has_human_confirm: bool，是否有真实人工确认回调（区分 user_denied /
            auto_denied）——按构造标志而非回调身份判断，因为 spawn 会包装回调
    """

    def __init__(self, *, middleware, agent_type: str, session_id: str,
                 instance_id: str, has_human_confirm: bool):
        """
        Args:
            middleware: list，安全中间件链
            agent_type: str，Agent 类型标识
            session_id: str，会话标识
            instance_id: str，实例唯一标识（写占用按它认持有者）
            has_human_confirm: bool，构造时是否有真实人工确认回调
        """
        self._middleware = middleware
        self._agent_type = agent_type
        self._session_id = session_id
        self._instance_id = instance_id
        self._has_human_confirm = has_human_confirm

    async def execute(self, tool_call: dict, *, trace_id: str | None,
                      turn: int = 0, confirm_lock: asyncio.Lock | None = None,
                      tools: dict, confirm_callback,
                      stream_sink) -> ToolResult:
        """
        执行单个 LLM 请求的工具调用，内部处理所有异常。

        流程(中间件管道)::

            0. 发射 tool_start 事件（门控 stream_sink；带工具名与已解析参数）
            1. 构造 ToolContext（trace_id / session_id / agent_type / 工具 / 参数）
               —— ctx 在参数解析前构造，保证所有路径都能走 after 链审计
            2. _run_parsed 走解析后的管道：
               a. 解析 JSON 参数（失败 → 走 after 链 → 错误 ToolResult）
               b. 非 dict 参数归一化为 {}（防止 ** 展开崩溃，审计记录空参数）
               c. 写类工具采样目标旧文本（diffstat 徽标数据源）
               d. 未知工具（不存在 → 走 after 链 → 错误 ToolResult）
               e. before 阶段：顺序执行各中间件的 before 钩子
                  - 抛 ConfirmRequired → 调用 confirm_callback 决策：
                    拒绝 → user_denied ToolResult；通过 → 执行工具
                  - 抛其他 SecurityError → security_blocked ToolResult
               f. 执行工具（异常 → ToolResult(text="Tool error: ...")）
               g. after 阶段：逆序执行各中间件的 after 钩子（洋葱模型）
            3. finally 收口发射恰好一条 tool_end 事件（耗时 + 写类 diffstat +
               completion/错误兜底文本）——正常/异常/拦截/解析失败全覆盖

        注意:JSON 解析失败和未知工具不绕过中间件管道——ctx 在解析前构造,
        早退路径也走 after 链(仅审计),保证这些异常路径同样留下审计痕迹
        (工具为 None 时各中间件 before 钩子不执行,只有审计记录调用)。

        错误处理采用"降级为文本"策略:所有异常(JSON 解析失败、未知工具名、工具
        执行异常、中间件拦截)都转为 ToolResult(text="..."),作为正常对话流的一部分
        反馈给 LLM。LLM 在下一轮中看到错误文本后可自行决定重试、调整参数或放弃。

        Args:
            tool_call: LLM 返回的工具调用字典 {"id": str, "function": {"name": str,
                "arguments": str}}，其中 arguments 为 JSON 字符串，本方法负责解析
            trace_id: 本次 run 的追踪 ID，写进 ToolContext 并决定运行期容器
            turn: ReAct 轮次，注入审计条目供跨轮回溯
            confirm_lock: 并发确认串行锁（asyncio.Lock | None）。同一 message 的多个
                工具调用并发执行时由调用方传入同一个锁，把确认回调调用串行化（CLI
                标准输入并发读会竞态）；None = 非并发路径
            tools: dict，本次调用的工具表（调用方现取——它可能被整表替换）
            confirm_callback: 用户确认回调（调用方现取——spawn 会把它包一层）
            stream_sink: 事件回调（调用方现取——REPL 在构造之后才挂上）；None 时
                零开销：既不解析参数用于展示，也不发事件

        Returns:
            ToolResult，始终返回（不抛异常）

        """
        name = tool_call["function"]["name"]

        # 0. 发送工具调用开始事件（流式渲染）
        # 目的：在参数解析前就发出事件，这样即使后续 JSON 解析失败或未知工具，
        # 终端渲染器也能及时清空中间内容缓冲区，避免把思考文本误判为最终答案。
        # 事件只带已解析的原始参数（tool_name + args），活动行摘要与调用行由终端
        # 层现算——引擎不做排版。参数非法或为空时传 None，终端显示纯工具名。
        # 门控：仅当挂了 stream_sink 时才执行（即 CLI 交互模式），否则零开销。
        if stream_sink is not None:
            try:
                parsed_args = json.loads(tool_call["function"]["arguments"])
            except (json.JSONDecodeError, TypeError):
                parsed_args = None
            if not isinstance(parsed_args, dict) or not parsed_args:
                parsed_args = None
            stream_sink(AgentEvent(
                AgentEventKind.TOOL_START, "", self._agent_type,
                tool_name=name, args=parsed_args))

        # 1. 按工具名查找 Tool 实例
        # 若 tools 中无此名称，说明 LLM 幻觉或受提示注入攻击生成了非法工具名。此时 tool = None，后续会处理并返回错误 ToolResult。
        tool = tools.get(name)

        # 2. 构建 ToolContext（工具调用上下文对象）
        # 提前构造 ctx 是设计关键：即使参数解析失败或工具不存在，也要能够执行 after 钩子（审计、日志等），保证所有执行路径都有审计记录。
        # ctx 会贯穿整个管道，中间件可在 before/after 中读写其属性（如添加额外元数据、修改结果等）。
        ctx = ToolContext(
            trace_id=trace_id,
            session_id=self._session_id,
            agent_type=self._agent_type,
            tool=tool,
            tool_name=name,
            timestamp=datetime.now().isoformat(),
            started_at=time.monotonic(),
            turn=turn,
        )

        try:
            return await self._run_parsed(tool_call, ctx, tool, tools,
                                      confirm_callback, confirm_lock, turn)
        finally:
            # tool_end 恰好一条：正常/异常/拦截/解析失败都经 finally 收口
            if stream_sink is not None:
                stream_sink(self._tool_end_event(ctx))

    async def _run_parsed(
        self, tool_call: dict, ctx: ToolContext, tool, tools,
        confirm_callback, confirm_lock, turn
    ) -> ToolResult:
        """解析后的管道；tool_end 收口在调用方 finally。

        Args:
            tool_call: dict，工具调用（含 function.arguments）
            ctx: ToolContext，调用上下文
            tool: Tool | None，命中的工具实例
            tools: dict，本次调用的工具表（写互斥拒绝时给模型回报可用工具）
            confirm_callback: 用户确认回调（本次调用现取）
            confirm_lock: asyncio.Lock | None，确认串行锁
            turn: int，当前轮次

        Returns:
            该次工具调用的 ToolResult（解析失败/未知工具/被拦截都降级为结果文本）。
        """

        # 3. 解析 JSON 参数
        # LLM 生成的 arguments 是 JSON 字符串，必须解析为 dict。
        # 若解析失败（非法 JSON），记录错误到 ctx，执行 after 钩子，然后返回带有解析错误的 ToolResult，让 LLM 自己决定是否重试。
        try:
            raw_args = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError as e:
            # LLM 生成了非法 JSON → 走 after 链记录审计后反馈给 LLM
            ctx.error = e
            await self._run_after_hooks(ctx)
            return ToolResult(text=f"Tool argument parse error: {e}")

        # 4. 参数归一化
        # 如果 raw_args 不是 dict（例如 LLM 生成了数组或字符串），为了安全将其归一化为空字典 {}，防止后续 tool.execute(**ctx.args) 时展开崩溃。
        # 这种异常情况也会被记录在 ctx.args 中供审计。
        ctx.args = raw_args if isinstance(raw_args, dict) else {}

        # 4.5 写类工具采样旧文本（diffstat 徽标数据源；读失败 → 无徽标，不影响执行）
        if ctx.tool_name in {"write_file", "edit_file"} and isinstance(ctx.args, dict) and ctx.args:
            target = (tool.effective_target_path(ctx.args)
                      if tool is not None else ctx.args.get("path"))
            if isinstance(target, str):
                ctx.diffstat_old = _read_text_or_none(target)

        # 并行写同一个文件会引出三件事，由三把不同的锁各管一段。把粒度与归属写清楚，
        # 免得后续把某一层误当成另一层：
        #
        # 1）同一 agent 内部的多次同路径写——丢写竞态 + 重复确认。两个协程对同一文件并发读-改-写，
        #    后写者会覆盖先写者；两个工具又各自弹确认框，用户输入的「a」（本会话放行）只覆盖先弹框的
        #    那个，后到者会再弹一次（而且两个协程并发读 CLI 标准输入本身就是竞态）。解法是模块级按路径
        #    注册表 `path_lock`：只有写类工具（side_effects 含文件写）才走这个加锁分支，按
        #    `effective_target_path` 取该路径的 `asyncio.Lock`，且锁要包住 `_run_guarded` **整体**
        #    （确认决策 + 执行，含 before 钩子里的 ConfirmRequired → confirm_callback → 工具
        #    执行），不能只包执行那一步——这是设计关键，所以 `async with` 放在 `execute` 这一层包住
        #    调用，而不是放进 guarded 函数内部。第二个并发调用因此在锁上等到第一个完整走完「用户确认 +
        #    写文件」；它拿到锁时授权键已进已授权集合，于是不再弹框直接放行——既消除重复弹框，又把同路径
        #    写串行化，丢写竞态随之消失。键经 `tool.effective_target_path` 导出而非直接读 `args["path"]`：
        #    write_file 的 filename 便捷入口没有 path 参数，须先组合出落盘路径再上锁——否则 pathless
        #    调用绕过锁，两类竞态经此入口复发。
        # 2）多人同时抢 CLI 标准输入（与文件路径无关）。`confirm_lock` 是 run 层传入、每次 run 一把，
        #    串行化所有并发工具调用的 confirm_callback 本身。
        # 3）跨 agent 的同路径冲突——不排队，当场拒。写工具在 `RunState.writing_paths` 上按**真实写目标**
        #    登记占用，另一个实例正在写同一路径时**当场拒绝**：等待结束仍然要写，那只是「带额外步骤的
        #    丢更新」，本仓要防的正是跨子 agent 的静默丢失，所以让第二个写根本不发生。登记是纯同步字典
        #    操作（无 await），所以能在这层 fail-fast，不会像中间件那样先排到路径锁后面才拒。
        #
        # 三者互补：`path_lock` 管同一个 agent 自己的多次调用排队，`confirm_lock` 管输入通道，
        # 写占用管跨实例。写者身份用 `side_effects` 判定；写占用键复用 `effective_target_path`——它已是
        # 确认键、串行锁与 diffstat 的唯一权威，对交付物写者返回的就是写目标（不做第二套路径概念）。
        target = tool.effective_target_path(ctx.args) if tool is not None else None
        owner = self._instance_id
        claimed = isinstance(target, str) and is_writer(tool)
        rs = get_run_state(ctx.trace_id) if claimed else None
        if claimed:
            holder = rs.claim_write(target, owner)
            if holder is not None:
                # 拒绝分支在 try 之外：本次没登记成功，不能走释放（否则会摘掉别人的占用）。
                # 仍走 after 链，让这次拒绝在审计里留痕（与 JSON 解析失败的早退同款）。
                ctx.error = RuntimeError(f"写互斥：{target} 正被另一实例占用")
                await self._run_after_hooks(ctx)
                return ToolResult(text=(
                    f"目标文件正被另一个并发子任务写入：{target}。"
                    "不要改文件名另写一份；请稍后原样重试，或把该冲突上报给上级。"),
                    is_error=True)
        try:
            if isinstance(target, str) and is_writer(tool):
                async with _path_lock(target):
                    return await self._run_guarded(tool, ctx, tools, confirm_callback, confirm_lock, turn)
            return await self._run_guarded(tool, ctx, tools, confirm_callback, confirm_lock, turn)
        finally:
            # 异常、取消、用户拒绝都会走到——所以占用不需要 TTL 兜底。
            # 取消场景有一处已知的宽松：占用在此释放时，`asyncio.to_thread` 起的 worker 线程可能仍在跑
            # 那次写——`to_thread` 不可取消，取消协程不会停掉线程。这可以接受：写是原子替换（不会留下
            # 写了一半的文件），且 Ctrl+C 取消的是整棵 agent 树，不会有别的写者趁这段时间进来。代价是
            # 「占用在手」在取消下不严格等于「写在进行」——不要依赖这个等价做判断。
            if claimed:
                rs.release_write(target, owner)

    async def _run_guarded(self, tool, ctx, tools, confirm_callback,
                           confirm_lock, turn) -> ToolResult:
        """确认与执行段：同路径锁保护下运行。

        Args:
            tool: Tool | None，命中的工具实例
            ctx: ToolContext，调用上下文
            tools: dict，本次调用的工具表（未知工具时回报可用清单）
            confirm_callback: 用户确认回调（本次调用现取）
            confirm_lock: asyncio.Lock | None，确认串行锁
            turn: int，当前轮次

        Returns:
            该次工具调用的 ToolResult（含 before 拦截结果与执行结果）。
        """

        # 5. 处理未知工具（LLM 幻觉或 prompt injection）
        # 若工具不存在（tool is None），记录错误，执行 after 钩子，并返回可用工具列表，帮助 LLM 纠正。
        # 注意：此时不会执行 before 钩子（因为无工具可执行），但 after 钩子仍会运行，确保审计覆盖。
        if tool is None:
            ctx.error = ValueError(f"Unknown tool: {ctx.tool_name}")
            await self._run_after_hooks(ctx)
            return ToolResult(
                text=f"Unknown tool: {ctx.tool_name}. Available: {list(tools.keys())}"
            )

        # 6. before 阶段：顺序执行所有安全中间件的 before 钩子。
        result = await self._run_before_hooks(ctx, confirm_lock, confirm_callback)
        if result is not None:
            # 若 before 阶段返回了 ToolResult（拒绝/拦截），直接返回给 LLM
            return result

        # 7. 执行工具逻辑（结果统一规范化为 ToolResult）
        # 经过 before 阶段后，确认工具可以执行。
        # 使用 asyncio.to_thread 将工具放到线程池执行，避免阻塞事件循环。
        # 对于 CPU/网络密集型操作，这能保证并发调度不被单个长耗时任务阻塞。
        #
        # 特殊处理：若工具声明了 wants_run_state=True（搜索去重池与写盘类工具），则注入一个按 trace_id 键控的运行期容器（_run_state），用于跨调用共享已访问的 URL、产物账本等在途状态。
        # 该注入通过额外参数 _run_state 传递，不写入 ctx.args，因为 ctx.args 会被序列化用于审计，而该容器不可序列化。
        try:
            # spawn 等异步原生工具：await tool.aexecute(...)，父事件循环上直接 await
            # asyncio.to_thread 把同步 execute 丢进线程池跑。这对普通工具没问题，但对 spawn 是致命的：asyncio 取消不了线程。
            # 用户按 Ctrl+C 时，事件循环收到 CancelledError，to_thread 的 future 被取消，
            # 但已经在线程里跑起来的子 agent 会继续跑完——留下一个孤儿线程，继续烧 LLM token、继续写文件，用户以为已经停了。
            #
            # aexecute 构造子 Agent 后，通过 _run_child 在父的事件循环上 await child.run(task)。
            # asyncio 的取消是协作式的，沿 await 链传播：父 run() 被取消 →
            # CancelledError 传入 spawn 调用点的这个 await →
            # 进入 _run_child（其 docstring 明确写了“取消传播给子任务，再原样上抛”）→
            # 子 agent 的 run() 也被取消。整棵 agent 树一起退出，不需要任何额外的清理逻辑。
            if getattr(tool, "async_execute", False):
                # 异步工具（如 spawn）：在当前事件循环上直接 await——子 agent 与父
                # 同循环，父被取消（Ctrl+C）时 CancelledError 沿 await 链级联传播，
                # 整棵 agent 树一起终止，不再经 to_thread 留孤儿线程。
                raw = await tool.aexecute(**ctx.args)

            # 需要运行期状态的工具（搜索去重池 / 写盘产物登记）：注入本次 run 的容器。
            # 子 agent 继承父 trace_id，所以同一用户任务内跨 agent 共享同一个容器。
            elif getattr(tool, "wants_run_state", False):
                raw = await asyncio.to_thread(
                    tool.execute, **ctx.args, _run_state=get_run_state(ctx.trace_id))

            # 普通同步工具：asyncio.to_thread(execute, ...)
            else:
                raw = await asyncio.to_thread(tool.execute, **ctx.args)
            ctx.result = raw if isinstance(raw, ToolResult) else ToolResult(text=str(raw))
        except Exception as e:
            # 工具执行失败（网络超时、文件不存在等）→ 反馈给 LLM
            ctx.error = e
            ctx.result = ToolResult(text=f"Tool error: {e}")

        # 8. after 阶段（逆序 = 洋葱模型，后注册的中间件先看到结果）
        await self._run_after_hooks(ctx)
        # 完成摘要（写/编辑工具）不再单独发事件：并入调用方 finally 收口的
        # tool_end 事件文本（_tool_end_event），保证每条工具调用恰好一条收口。
        return ctx.result

    def _tool_end_event(self, ctx: ToolContext) -> AgentEvent:
        """tool_end 事件：耗时 + 写类 diffstat + 结果文本（结构化，不排版）。

        completion 与 error 都原样上报，由终端决定这一行怎么显示——引擎不拼
        "Tool failed: …" 这类文案。diffstat 是例外：±行数需要「写盘前的旧文本」
        这一现场（在 before 钩子之前采样），写完就取不回来了，终端事后算不出，
        故只能在这里算好。

        Args:
            ctx: ToolContext，本次工具调用上下文

        Returns:
            tool_end 事件（耗时 + 写类 diffstat + completion/error 原文）。
        """
        duration = (int((time.monotonic() - ctx.started_at) * 1000)
                    if ctx.started_at is not None else None)
        diffstat = None
        if ctx.tool_name in {"write_file", "edit_file"} and ctx.diffstat_old is not None:
            target = (ctx.tool.effective_target_path(ctx.args)
                      if ctx.tool is not None else ctx.args.get("path"))
            new_text = _read_text_or_none(target) if isinstance(target, str) else None
            if new_text is not None:
                added, removed = _diffstat(ctx.diffstat_old, new_text)
                diffstat = (target, added, removed)
        completion = (ctx.result.completion
                      if ctx.result is not None and ctx.result.completion else None)
        return AgentEvent(AgentEventKind.TOOL_END, "", self._agent_type,
                          tool_name=ctx.tool_name or None,
                          args=ctx.args if isinstance(ctx.args, dict) and ctx.args else None,
                          completion=completion,
                          error=str(ctx.error) if ctx.error is not None else None,
                          duration_ms=duration, diffstat=diffstat)

    async def _run_before_hooks(self, ctx: ToolContext,
                               confirm_lock: asyncio.Lock | None = None,
                               confirm_callback=None) -> ToolResult | None:
        """顺序执行所有安全中间件的 before 钩子。
        每个中间件可以：
          - 正常返回：放行，继续下一个中间件
          - 抛出 ConfirmRequired：需要用户确认（高风险操作）
          - 抛出 SecurityError（或其子类）：策略拦截（如拒绝访问敏感文件）

        若所有钩子通过，返回 None，表示可以继续执行工具。
        若中途拦截（用户拒绝、策略阻止），则返回一个 ToolResult，调用方应直接返回该结果，不再执行工具。

        内部处理：
            - 捕获 ConfirmRequired → 调用确认回调（串行化），记录审批事件
            - 捕获 SecurityError → 直接返回策略拒绝的 ToolResult

        Args:
            ctx: ToolContext，调用上下文
            confirm_lock: asyncio.Lock | None，确认串行锁

        Returns:
            None 表示全部通过可执行工具；否则返回拦截/拒绝的 ToolResult。
        """
        for mw in self._middleware:
            try:
                await mw.before(ctx)
            except ConfirmRequired as cr:
                # ----- 需要用户确认 -----
                # 先通知中间件“请求已发出”
                for mw in self._middleware:
                    await mw.on_approval(ctx, "requested")

                # 定义异步决策函数，调用外部确认回调
                async def _decide() -> bool:
                    """调用外部确认回调获取用户决策。

                    Returns:
                        True 表示用户放行。
                    """
                    return await confirm_callback(cr)

                # 串行化确认（若提供了锁）
                try:
                    if confirm_lock is not None:
                        async with confirm_lock:
                            confirmed = await _decide()
                    else:
                        confirmed = await _decide()
                except asyncio.CancelledError:
                    # 取消路径审计闭环：approval_requested 已发出但决策
                    # 未落——显式结算为 cancelled 后再抛，审计不再出现「有 requested
                    # 无 decided」的悬空；确认集合不加键（下次同路径仍会询问）。
                    for mw in self._middleware:
                        await mw.on_approval(
                            ctx, "decided",
                            approval_outcome=ApprovalOutcome.CANCELLED)
                    raise

                # 根据确认结果和是否有人工回调，标记决策类型
                # 决策语义:确认通过 → user_confirmed;被拒时按是否有真实人工回调区分
                # user_denied / auto_denied(fail-safe 默认拒绝,无人值守)。
                outcome = (
                    ApprovalOutcome.USER_CONFIRMED if confirmed else (
                        ApprovalOutcome.USER_DENIED if self._has_human_confirm
                        else ApprovalOutcome.AUTO_DENIED))
                ctx.approval_outcome = outcome

                # 通知中间件决策已做出
                for mw in self._middleware:
                    await mw.on_approval(ctx, "decided", approval_outcome=outcome)

                if not confirmed:
                    # 拒绝 → 记录错误并走 after 钩子，反馈给 LLM
                    # 注意：此时工具尚未执行，但 after 钩子仍会运行以记录审计。
                    # summary.decision 用已计算的 outcome（user_denied/auto_denied），
                    # 与 ctx.approval_outcome 保持一致——拒绝路径带决策依据回放给 LLM
                    ctx.error = cr
                    await self._run_after_hooks(ctx)
                    return ToolResult(
                        text=f"User denied: {cr.tool_name}",
                        summary={SUMMARY_DECISION: outcome, "tool": cr.tool_name},
                    )

                # 确认通过：标记 ctx，继续执行（不返回，后续会执行工具）
                cr.confirm()
                ctx.user_confirmed = True
            except SecurityError as se:
                # ContentGuardMiddleware / WorkspacePolicyMiddleware /
                # CommandGuardMiddleware 抛出的 security_blocked → 带决策摘要返回
                ctx.error = se
                await self._run_after_hooks(ctx)
                return ToolResult(
                    text=f"{se.decision}: {se.reason}",
                    summary={SUMMARY_DECISION: se.decision,
                             SUMMARY_VIOLATIONS: getattr(se, "violations", [])},
                )
        # 全部通过
        return None

    async def _run_after_hooks(self, ctx: ToolContext) -> None:
        """逆序执行所有中间件的 after 钩子（洋葱模型）。

        无论工具是否执行成功、无论是否被 before 拦截，
        只要进入了管道（ctx 已构建）就会执行 after，
        保证审计等横切关注点在所有路径上都能记录。

        Args:
            ctx: ToolContext，调用上下文（含结果或异常）
        """
        for mw in reversed(self._middleware):
            try:
                await mw.after(ctx)
            except Exception as e:
                # 审计等 after 钩子失败不应中止工具执行结果返回
                print(f"[security] after hook {type(mw).__name__} failed: {e}", file=sys.stderr)
