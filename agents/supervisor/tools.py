"""Supervisor 调度工具（spec §3）——4 个仅调度类工具，不进 paperflow/tools/。

归属声明：与 ReviewDraftTool 同款刻意例外——agent 专属、需 parent 注入
（needs_parent），SpawnSubAgentTool 是 Layer 3 ReviewDraftTool 的升级壳。
Supervisor 是唯一拥有 spawn 工具的 agent（权限最小化：SubAgent 无递归调度）。
"""
import asyncio
import json
import threading
import time

from pydantic import BaseModel

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, StreamEvent
from paperflow.core.tool import Tool, ToolResult


class SubAgentResult(BaseModel):
    """子 agent 结构化结果（ADR 0003 第 3 层）。

    status ∈ {success, failed, timeout, denied}；needs_attention 独立标志
    （denied + needs_attention=True 表示"被拒且需用户介入"，与 failed 可重试可区分）。
    """
    status: str
    summary: str
    error_detail: str = ""
    needs_attention: bool = False


def _check_spawn_allowed(parent: Agent, agent_type: str) -> str | None:
    """allowed_spawns 运行时校验（ADR 0003 第 3 层）——Spawn/Parallel 两个工具共用。

    supervisor 硬编码放行；非 supervisor（未来若有 spawn 工具配置）越界 spawn
    返回错误信息（调用方映射为 denied），放行返回 None。

    共享原因：审阅发现（R1）ParallelSpawnTool._run_one 曾跳过此校验、无条件构造
    child——两个 spawn 工具不对称。抽成单点校验保证二者永远一致：当前仅 supervisor
    拥有 spawn 工具，此分支为未来预留——denied 有真实代码路径（非死代码）。
    """
    if parent.agent_type == "supervisor":
        return None
    cfg = parent.agent_registry.get_config(parent.agent_type)
    if agent_type not in cfg.allowed_spawns:
        return f"{parent.agent_type} 不能 spawn {agent_type}"
    return None


class _UserWaitClock:
    """用户确认等待计时器：confirm_callback 等待期间累积，供子 agent 超时预算扣除。

    语义（2026-08-07 用户决策"确认时间排除在超时外"）：用户确认是交互等待，不应计入
    子 agent 的执行预算。预算 = 基础 timeout + 已累积的用户等待——子 agent 卡在确认上
    时预算持续延长（一直等用户），纯执行超时仍正常触发。

    begin/end 而非"结束才记"：预算循环要看到**进行中**的等待（只记结束时，确认进行中
    total 为 0，预算会误以为没在等用户而误杀）。total() 返回已完成 + 进行中的和。
    线程安全：confirm wrapper 与预算循环在同一事件循环线程（parallel 共享 gather loop、
    single 是 spawn worker loop），防御性加锁防未来多线程变化。"""
    def __init__(self) -> None:
        self._completed = 0.0
        self._active_start: float | None = None   # 确认进行中的 monotonic 起点
        self._lock = threading.Lock()

    def begin(self) -> None:
        """确认等待开始（wrapper 进入 orig 前调用）。"""
        with self._lock:
            if self._active_start is None:
                self._active_start = time.monotonic()

    def end(self) -> None:
        """确认等待结束（wrapper finally 调用）——把进行中时长并入已完成。"""
        with self._lock:
            if self._active_start is not None:
                self._completed += time.monotonic() - self._active_start
                self._active_start = None

    def total(self) -> float:
        """当前总用户等待 = 已完成 + 进行中（预算循环每轮据此重算剩余预算）。"""
        with self._lock:
            active = (time.monotonic() - self._active_start
                      if self._active_start is not None else 0.0)
            return self._completed + active


def _wrap_confirm_callback(orig, clock: _UserWaitClock):
    """包装 confirm_callback：外包计时，把等待时长记入 clock（预算据此延长）。

    原回调（如 CLI 的 _stdin_confirm）语义不变——只加 begin/end 计时。finally 保证
    无论确认/拒绝/异常都停止计时（不把用户等待泄漏到后续工具的执行预算）。"""
    async def wrapped(cr):
        clock.begin()
        try:
            return await orig(cr)
        finally:
            clock.end()
    return wrapped


async def _run_child_with_budget(coro, timeout: float, clock: _UserWaitClock):
    """运行子 agent coro，预算 = timeout + 用户确认等待累积（确认期间超时暂停）。

    替代 asyncio.wait_for(coro, timeout)：wait_for 是纯墙钟，确认等待会吃掉预算导致
    "用户忘确认→任务超时误杀"（2026-08-07 根因）。本函数每轮重算剩余 =
    (基础 deadline + 累积用户等待) - now；≤0 才取消并抛 asyncio.TimeoutError
    （与既有 except asyncio.TimeoutError 分支对齐）。子 agent 卡在确认上时 clock.total()
    持续增长 → 剩余预算为正 → 一直等用户，不误杀；纯执行超时（无等待兜底）仍触发。

    实现用 asyncio.wait({task}, timeout) 轮询：超时一轮只是本轮 wait 到期，task 继续
    运行未取消；下一轮重算剩余（累积等待即时反映进预算）再等。task 完成则返回其结果
    （异常原样上抛，如 MaxTurnsExceeded → 既有 except Exception 映射 failed）。"""
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)
    base_deadline = loop.time() + timeout
    while not task.done():
        remaining = (base_deadline + clock.total()) - loop.time()
        if remaining <= 0:
            # 纯执行超时（无用户等待兜底）→ 取消子 agent，抛 TimeoutError
            task.cancel()
            try:
                await task
            except BaseException:
                pass
            raise asyncio.TimeoutError()
        done, _ = await asyncio.wait({task}, timeout=remaining)
        if done:
            return task.result()
    return task.result()


class SpawnSubAgentTool(Tool):
    """派发单个 SubAgent，返回 SubAgentResult 序列化。"""

    name = "spawn_sub_agent"
    description = ("派发单个 SubAgent 执行子任务，返回结构化结果（status/summary/error_detail/"
                   "needs_attention）。失败可依据 error_detail 决定重试或上报。")
    parameters = {
        "type": "object",
        "properties": {
            "agent_type": {"type": "string", "description": "目标 SubAgent 类型，如 search-paper"},
            "task": {"type": "string", "description": "子任务文本（含实体，已拼入上下文）"},
        },
        "required": ["agent_type", "task"],
    }
    #: 需要父 Agent 引用（Agent.__init__ 只注入声明者）
    needs_parent = True
    risk_level = "low"
    #: 子 agent 超时秒数（fallback 默认；config.agent_timeouts 命中时被覆盖）。
    #: 类属性保留——既是无 config 时的 fallback，也是测试覆盖点（M3：测例设 0.05s）。
    timeout = 120

    def __init__(self, agent_timeouts: dict[str, int] | None = None):
        # 按 agent 超时覆盖表（D2）：config.agent_timeouts 注入。测试直接构造
        #（无 map）→ fallback 到类属性 timeout——覆盖 0.05s 的既有测例不破坏。
        self._agent_timeouts = agent_timeouts or {}

    def _resolve_timeout(self, agent_type: str) -> int:
        """解析该 agent 生效超时：config 命中优先，否则类默认（M3 seam 保留）。"""
        return self._agent_timeouts.get(agent_type, self.timeout)

    def execute(self, agent_type: str, task: str) -> ToolResult:
        parent = self._parent
        # ① allowed_spawns 运行时校验（ADR 0003 第 3 层）：抽成 _check_spawn_allowed
        #    共享 helper（ParallelSpawnTool._run_one 同款调用，防两工具校验漂移）。
        #    supervisor 硬编码放行；非 supervisor 越界 spawn → denied（有真实代码路径）。
        denied = _check_spawn_allowed(parent, agent_type)
        if denied is not None:
            result = SubAgentResult(status="denied", summary=denied)
            return ToolResult(text=result.model_dump_json(), summary=result.model_dump())

        # ② 构造 child：继承 security_middleware + session_id（同一审计链）+ confirm_callback
        #    （D6 关键：generate-note 的写盘工具 requires_confirm=True，不传则 _default_confirm
        #    始终拒绝，spawn 出的 generate-note 永远写不出笔记）。
        #    不传 intent_pipeline/session → child 的 intent_enabled=False（D3）。
        child = Agent(
            llm=parent.llm, agent_registry=parent.agent_registry,
            agent_type=agent_type, security_middleware=parent.security_middleware,
            session_id=parent.session_id, confirm_callback=parent.confirm_callback,
            # 流式透传：单 spawn 子 agent 继承父 stream_callback，其推理 token
            # 带自己的 agent_type 实时流式（CLI 渲染为 child 分段）。
            # getattr（而非 parent.stream_callback）让 mock / 真实 Agent 都可用。
            stream_callback=getattr(parent, "stream_callback", None),
        )
        # 传解析后的超时：_run_child 用实际生效值（config > 类默认）
        return self._run_child(child, agent_type, task)

    def _run_child(self, child: Agent, agent_type: str, task: str) -> ToolResult:
        """执行 child.run 并映射为 SubAgentResult（success/timeout/denied/failed）。

        asyncio.run 桥接：execute 跑在 to_thread worker 线程（无 running loop），
        必须 asyncio.run 新建事件循环（Layer 3 spec §4.3 钉死，嵌套会抛 RuntimeError）。
        """
        timeout = self._resolve_timeout(agent_type)
        # 用户确认等待不计入执行预算（2026-08-07）：包装 child.confirm_callback 记录等待
        # 时长，_run_child_with_budget 把累积等待加回剩余预算——确认期间子 agent 不被
        # 超时误杀（写盘等用户确认时一直等）。纯执行超时仍正常触发。child 的 confirm
        # 回调是构造时继承的（confirm_callback=parent.confirm_callback），此处只外包计时。
        clock = _UserWaitClock()
        child.confirm_callback = _wrap_confirm_callback(child.confirm_callback, clock)
        try:
            text = asyncio.run(_run_child_with_budget(child.run(task), timeout, clock))
            result = SubAgentResult(status="success", summary=text)
        except asyncio.TimeoutError:
            result = SubAgentResult(status="timeout", summary="子任务执行超时",
                                    # M3：插值解析后的 timeout（config 命中时非类默认）
                                    error_detail=f"SubAgent 在 {timeout}s 内未完成")
        except PermissionError as e:
            # 防御性分支（spec ⚪2）：当前架构 child 的 _exec_tool 把 PolicyDenied/
            # SecurityBlocked degrade-to-text 吸收，不向上抛——几乎不会触发。对齐
            # ADR 0003 保留，implementer 不要据此推导存在真实路径。
            result = SubAgentResult(status="denied", summary="子任务被策略引擎拒绝",
                                    error_detail=str(e), needs_attention=True)
        except Exception as e:
            result = SubAgentResult(status="failed", summary="子任务执行失败",
                                    error_detail=str(e))
        return ToolResult(text=result.model_dump_json(), summary=result.model_dump())


class ParallelSpawnTool(Tool):
    """并行派发多个 SubAgent，逐 child 隔离——一个失败不拖垮其他（spec 🟠3）。"""

    name = "parallel_spawn"
    description = ("并行派发多个 SubAgent，返回 SubAgentResult 列表。各子任务独立——"
                   "一个失败不影响其他。注意：都打 RAG 时并行度在 RAG 锁边界封顶。")
    parameters = {
        "type": "object",
        "properties": {
            "spawns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_type": {"type": "string"},
                        "task": {"type": "string"},
                    },
                    "required": ["agent_type", "task"],
                },
            },
        },
        "required": ["spawns"],
    }
    needs_parent = True
    risk_level = "low"
    timeout = 120

    def __init__(self, agent_timeouts: dict[str, int] | None = None):
        # 与 SpawnSubAgentTool 对称（R1 哲学：两 spawn 工具永不漂移）
        self._agent_timeouts = agent_timeouts or {}

    def _resolve_timeout(self, agent_type: str) -> int:
        return self._agent_timeouts.get(agent_type, self.timeout)

    def execute(self, spawns: list[dict]) -> ToolResult:
        parent = self._parent

        async def _run_one(agent_type: str, task: str) -> SubAgentResult:
            # ① 与 SpawnSubAgentTool 同款 allowed_spawns 校验（R1：两 spawn 工具对称）。
            #    越界 spawn 返回 per-child denied——不构造 child、不拖垮其他 child
            #    （per-child 隔离语义：gather 只收集各 child 的独立 SubAgentResult）。
            denied = _check_spawn_allowed(parent, agent_type)
            if denied is not None:
                return SubAgentResult(status="denied", summary=denied)
            # ② 每个 spawn 独立构造 child（继承 confirm_callback，同 SpawnSubAgentTool）
            #    并行子 agent 各自在独立 to_thread 线程流式：多路 content token 并发会
            #    搅成一团，故丢弃 content 只透传 tool 行（行级、完整）并加 [agent_type]
            #    前缀；推理文本由 supervisor 汇总后在最终回答呈现。
            pcb = getattr(parent, "stream_callback", None)
            if pcb is None:
                child_cb = None
            else:
                def child_cb(ev: StreamEvent) -> None:
                    if ev.kind == "tool":
                        pcb(StreamEvent("tool", f"[{agent_type}] {ev.text}", ev.agent_type))
            child = Agent(
                llm=parent.llm, agent_registry=parent.agent_registry,
                agent_type=agent_type, security_middleware=parent.security_middleware,
                session_id=parent.session_id, confirm_callback=parent.confirm_callback,
                stream_callback=child_cb,
            )
            timeout = self._resolve_timeout(agent_type)
            # 同 _run_child：确认等待排除在超时预算外（2026-08-07）。并行场景下
            # confirm_callback（如 CLI 的 _stdin_confirm）跑在后台线程不冻结共享
            # gather loop——一个子 agent 等确认不影响其他子 agent 继续执行。
            clock = _UserWaitClock()
            child.confirm_callback = _wrap_confirm_callback(child.confirm_callback, clock)
            try:
                text = await _run_child_with_budget(child.run(task), timeout, clock)
                return SubAgentResult(status="success", summary=text)
            except asyncio.TimeoutError:
                return SubAgentResult(status="timeout", summary="子任务执行超时",
                                      error_detail=f"SubAgent 在 {timeout}s 内未完成")
            except Exception as e:
                return SubAgentResult(status="failed", summary="子任务执行失败",
                                      error_detail=str(e))

        async def _run_all() -> list[SubAgentResult]:
            # asyncio.gather 是普通函数（非 async def），必须在 running loop 内调用：
            # 在 asyncio.run 外部直接 gather 会经 get_event_loop() 取"当前 loop"，
            # Python 3.11 下拿不到可用 loop（pytest-asyncio 已 set 过又关闭）→
            # RuntimeError/ValueError。故包一层 _run_all，让 gather 在新建 loop 内执行。
            return await asyncio.gather(*[
                _run_one(s["agent_type"], s["task"]) for s in spawns
            ])

        # _run_one 内部全捕获 → gather 永不因单 child 失败 cancel（per-child 隔离）。
        # asyncio.run 桥接同 SpawnSubAgentTool（execute 跑在 to_thread worker 线程无 loop）。
        results = asyncio.run(_run_all())
        payload = [r.model_dump() for r in results]
        return ToolResult(text=json.dumps(payload, ensure_ascii=False),
                          summary={"count": len(payload)})


class AggregateResultsTool(Tool):
    """汇总 SubAgentResult 列表；needs_attention 标记呈现（规则 6）。纯文本不做决策。"""

    name = "aggregate_results"
    description = "汇总多个 SubAgentResult 为清晰列表；带 ⚠️ 标记的项需最终呈现给用户。"
    parameters = {
        "type": "object",
        "properties": {
            "results": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["results"],
    }
    # 纯文本汇总，无需 parent 引用
    risk_level = "low"

    def execute(self, results: list[dict]) -> ToolResult:
        lines = []
        for r in results:
            status = r.get("status", "?")
            needs = r.get("needs_attention", False)
            mark = " ⚠️" if needs else ""
            lines.append(f"- [{status}{mark}] {r.get('summary', '')}")
        text = "\n".join(lines) if lines else "(无结果)"
        return ToolResult(text=text)


class AskUserTool(Tool):
    """向用户确认信息（in-turn 阻塞，spec D4②）。

    经 parent.ask_user_callback 读 stdin；callback 为 None（程序化/测试）→
    fail-safe 返回"无法交互"（与 _default_confirm 同款，Supervisor ReAct 自行处理）。
    """

    name = "ask_user"
    description = "向用户提问并等待回答（阻塞直到用户输入）。答案作为工具结果返回。"
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "要问用户的问题"}},
        "required": ["question"],
    }
    needs_parent = True
    risk_level = "low"

    def execute(self, question: str) -> ToolResult:
        cb = getattr(self._parent, "ask_user_callback", None)
        if cb is None:
            # fail-safe：无法交互时明确告知，Supervisor 依据已有信息自行决策（不挂死）
            return ToolResult(text="无法交互：当前环境未提供用户回调，请基于已有信息决定")
        # cb 是 CLI 注入的 stdin 读（worker 线程 input() 可用，spec §3.5 线程注记）
        answer = cb(question)
        return ToolResult(text=f"用户回答：{answer}")


def _make_supervisor_tools() -> list:
    """装配 4 个调度工具。config 在 import 时构造（每进程静态，对齐 make_tools 惯例）；
    agent_timeouts 经 config.yaml 顶层注入 spawn 工具按 agent 解析超时。"""
    cfg = PaperFlowConfig.from_env()
    return [
        SpawnSubAgentTool(agent_timeouts=cfg.agent_timeouts),
        ParallelSpawnTool(agent_timeouts=cfg.agent_timeouts),
        AggregateResultsTool(),
        AskUserTool(),
    ]


# 注：supervisor 工具无 allowed_roots（无文件访问），无需 make_tools 装配——
# 直接实例化列表即可（AgentRegistry 约定 TOOLS 是 Tool 实例列表）。
TOOLS = _make_supervisor_tools()
