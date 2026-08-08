"""共享 spawn 工具层——SpawnSubAgentTool / ParallelSpawnTool 及配套 helper。

原属 agents/supervisor/tools.py（Layer 3 ReviewDraftTool 的升级壳），Task 1 纯搬移
抽到 paperflow/tools/：实现归共享层，装配仍只在 agents/supervisor/tools.py 的
_make_supervisor_tools——Supervisor 是唯一装配 spawn 工具的 agent（权限最小化，
SubAgent 无递归调度）。需 parent 注入（needs_parent），见 Tool 约定。
"""
import asyncio
import hashlib
import json
import re
import threading
import time

from pydantic import BaseModel

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, StreamEvent
from paperflow.core.structured import StructuredOutput
from paperflow.core.tool import Tool, ToolResult


class SubAgentResult(BaseModel):
    """子 agent 结构化结果（ADR 0003 第 3 层）。

    status ∈ {success, failed, timeout, denied}；needs_attention 独立标志
    （denied + needs_attention=True 表示"被拒且需用户介入"，与 failed 可重试可区分）。
    digest：C2 结构化摘要（StructuredOutput 从最终回答提取），supervisor 直接读它
    组织最终回答（取代 aggregate_results）；提取失败/超时落 {}（回退读 summary）。
    """
    status: str
    summary: str
    error_detail: str = ""
    needs_attention: bool = False
    digest: dict = {}


class SearchPaperDigest(BaseModel):
    """search-paper 结果摘要（C2）。

    supervisor 读它组织回答：命中多少篇、有哪些论文、哪些已下载、哪些待确认。
    pending_confirm / needs_attention 对应下载门禁的"待用户确认"路径——spawn 结果是
    denied 之外的第二处用户介入点，supervisor 需照此提示确认。
    """
    count: int
    papers: list[str]
    downloaded: list[str] = []
    pending_confirm: list[str] = []
    needs_attention: bool = False


class ReviewerDigest(BaseModel):
    """reviewer 结果摘要（C2）。

    verdict + pass/fail 计数让 supervisor 一眼判断裁决结论；download_list 承载
    reviewer 建议下载的论文清单（C2 消费点）。
    """
    verdict: str
    pass_count: int
    fail_count: int
    download_list: list[str] = []


class GenerateNoteDigest(BaseModel):
    """generate-note 结果摘要（C2）。

    note_path 即产物绝对路径（C1 约定"返回含笔记绝对路径即成功"），status 描述写盘结果。
    """
    note_path: str
    status: str


class GenericDigest(BaseModel):
    """未知 agent 类型的通用摘要（C2）。

    兜底 schema：任何新 agent 类型未注册时也能抽出 summary_short / key_items，避免
    supervisor 对未知类型无从下手；count 可空（非列表型 agent 无此维度）。
    """
    summary_short: str
    key_items: list[str] = []
    count: int | None = None


def digest_schema_for(agent_type: str) -> type[BaseModel]:
    """按 agent_type 映射 digest schema；未知类型落 GenericDigest（C2）。

    与 intent→agent 对照同源：spawn 侧按 agent_type 挑 schema，supervisor 按 agent_type
    解释 digest。新 agent 类型接入只需在此注册（如 answer-question 未注册走 GenericDigest）。
    """
    return {
        "search-paper": SearchPaperDigest,
        "reviewer": ReviewerDigest,
        "generate-note": GenerateNoteDigest,
    }.get(agent_type, GenericDigest)


async def _extract_digest(llm, agent_type: str, text: str) -> dict:
    """用 StructuredOutput 从子 agent 最终回答提取结构化摘要（C2）。

    复用 StructuredOutput 三层防御（json_mode + pydantic 校验 + 重试）；独立超时 30s
    （与子 agent 执行超时解耦——digest 提取是"锦上添花"，卡死不能拖垮 spawn 主流程）；
    失败/超时返回 {} 兜底（supervisor 回退读 summary，与未接入 digest 前行为一致）。
    截取 text[-2000:] 控制 prompt 长度——子 agent 最终回答可能很长（如 generate-note
    的整篇笔记内容），结构化摘要只需要结论性尾部。
    """
    try:
        digest = await asyncio.wait_for(
            StructuredOutput(llm).extract(
                prompt=f"从以下子 agent 最终回答提取结构化摘要：\n{text[-2000:]}",
                schema=digest_schema_for(agent_type)),
            timeout=30)
        return digest.model_dump()
    except Exception:
        return {}


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


#: spawn 去重注册表：session_id -> {task指纹: {"state": "running"|"done", "result": ToolResult|None, "started_at": float}}
#: B3 去重的机械安全网——同 session 同任务防重复派发。键为 session_id：去重只在同一
#: 会话内生效，跨会话互不影响（并行 supervisor 各自独立会话互不干扰）。指纹是**纯文本**
#: 的（sha256 规范化文本，零 I/O、不含内容快照）；done 缓存语义由 _task_has_path 门控：
#: 无路径任务（纯文本，世界不变）→ done<5min 可复用；有路径任务（引用真实文件，世界
#: 可变——子 agent 执行期间 edit_file 可能改它）→ 只 running 去重、完成即清条目，永不
#: 缓存 done（route 3 路径门控，见 execute）。
_SPAWN_REGISTRY: dict[str, dict[str, dict]] = {}
#: 注册表并发锁：execute 跑在 to_thread worker 线程，并行 spawn 同时读写注册表
#: → 所有访问持这把锁（单次 dict.get/set 虽原子，"检查命中-注册 running"两步
#: 必须整体原子，否则两线程同时注册各自派发一次——去重失效）。
_SPAWN_LOCK = threading.Lock()
#: done 结果可复用的时间窗（秒）：5 分钟内同指纹（仅无路径任务）spawn 直接复用缓存结果，
#: 超窗重跑——避免过期结果无限复用（世界变化后旧结果不该被当作新结果交付）。有路径任务
#: 完成即清条目、不写 done（world 可变，无 done 可复用）。
_SPAWN_REUSE_WINDOW_S = 300

#: 任务文本中绝对路径的启发式正则（_task_has_path 的布尔判据）：抓 "/" 开头、不含空白/
#: 中文标点/半角逗号分号冒号/引号的最长串。不再做提取/修剪（_extract_paths/_TRAILING 已删，
#: 不读文件内容）——只作「是否含路径」的布尔判断。排除集不含半角括号（file(v2).md 完整
#: 识别）；中文全角括号 `（）` 仍是分隔符。误判安全：散文里的 "/"（如 "/5 评分"）假阳性 →
#: 保守跳过 done 缓存 → 安全重跑；真路径假阴性风险低（模板路径跟在空格后，命中 (?<!\S)）。
_PATH_RE = re.compile(r"(?<!\S)/[^\s，,;:。（）\"']+")


def _task_fingerprint(task: str) -> str:
    """纯文本指纹 = sha256(规范化任务文本)[:16]（route 3 回退：不再含内容快照）。

    仅对空白/换行做规范化（"  a\n b  " → "a b"），不读任何文件 → 零 I/O。指纹不再感知
    文件内容变化：同文本恒同指纹。内容变化的正确性改由 _task_has_path 门控兜底——有路径
    任务完成即清条目、永不缓存 done（见 execute），无路径任务纯文本世界不变可直接缓存。
    """
    norm = " ".join(task.split())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def _task_has_path(task: str) -> bool:
    r"""任务文本是否含绝对路径（布尔判断，不提取）：_PATH_RE.search 命中即 True。

    门控语义（route 3，openclaw 式）：含路径的任务引用真实文件（世界可变——子 agent 执行
    期间 edit_file 可能改它），故只 running 去重、完成即清条目，永不缓存 done；无路径任务
    （纯文本）才允许 done<5min 缓存复用。误判安全方向：散文里的 "/"（如 "/5 评分"）被误判
    为路径（假阳性）→ 保守跳过 done 缓存 → 安全重跑；真路径漏判（假阴性）风险低——模板
    路径都跟在空格后，满足 (?<!\S) 前缀守卫。
    """
    return _PATH_RE.search(task) is not None


def _evict_stale_spawn_entries(reg: dict, now: float) -> None:
    """剔除注册表里已过复用窗的 done 条目（内存卫生）。

    长会话下 _SPAWN_REGISTRY 会按指纹数无限累积 done 条目（每条持有一个 ToolResult
    常驻内存），而超窗的旧结果本就不可再复用——留着纯占内存。规则：now - started_at
    > _SPAWN_REUSE_WINDOW_S 的 done 条目直接丢弃。**running 条目不删**：它可能正被
    另一 to_thread worker 线程执行中，删掉会让并发去重的「检查+注册」原子性失效
    （下一线程看不到 running 又派发一次）。调用方须在 _SPAWN_LOCK 内调用（读/写
    注册表都走这里，访问即顺手清理，无需单独定时任务）。
    """
    stale = [fp for fp, e in reg.items()
             if e.get("state") == "done"
             and now - e.get("started_at", now) > _SPAWN_REUSE_WINDOW_S]
    for fp in stale:
        reg.pop(fp, None)


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
                   "digest/needs_attention），digest 为子任务的结构化摘要（提取失败时为空）。"
                   "失败可依据 error_detail 决定重试或上报。")
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

        # ② B3 路径门控最小缓存（route 3，openclaw 式）：同一 session 同指纹去重。
        #    主防线是 SKILL「同一意图不重复 spawn」，此处是机械安全网。execute 跑在
        #    to_thread worker 线程，并行 spawn 并发访问注册表 → 检查+注册须持锁整体原子。
        #    门控规则（由 _task_has_path 区分）：
        #    - 无路径任务（纯文本，世界不变）→ running 提示 + done<5min 缓存复用（全量去重）
        #    - 有路径任务（引用真实文件，世界可变）→ 只 running 去重，完成即清条目、永不
        #      缓存 done——子 agent 执行期间 edit_file 可能已改文件，缓存旧结果会交付陈旧裁决
        fp = _task_fingerprint(task)
        has_path = _task_has_path(task)
        with _SPAWN_LOCK:
            reg = _SPAWN_REGISTRY.setdefault(parent.session_id, {})
            # 访问注册表即顺手清理超窗 done 条目（长会话防无限累积，见 _evict_stale_spawn_entries）
            _evict_stale_spawn_entries(reg, time.monotonic())
            hit = reg.get(fp)
            now = time.monotonic()
            if hit and hit["state"] == "running":
                return ToolResult(text="同任务正在执行中，请等待其结果（已去重，勿重复派发）")
            if hit and hit["state"] == "done" and not has_path \
                    and now - hit["started_at"] < _SPAWN_REUSE_WINDOW_S:
                return hit["result"]
            reg[fp] = {"state": "running", "result": None, "started_at": now}

        result = None
        try:
            # ③ 构造 child：继承 security_middleware + session_id（同一审计链）+ confirm_callback
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
            result = self._run_child(child, agent_type, task)
        finally:
            # 完成收尾（B3）：无路径写 done 供窗内复用；有路径/异常 → 清条目不缓存
            # （有路径任务世界可变永不缓存 done；result 为 None 表示构造/执行异常，防
            # None 入缓存污染后续复用）。has_path/fp 是 execute 内局部变量，finally 直接
            # 可见，注册表读写全在 _SPAWN_LOCK 内。
            with _SPAWN_LOCK:
                reg = _SPAWN_REGISTRY.setdefault(parent.session_id, {})
                # 完成写盘同样先清理超窗 done 条目（防长会话注册表无限膨胀）
                _evict_stale_spawn_entries(reg, time.monotonic())
                if result is None or has_path:
                    reg.pop(fp, None)
                else:
                    reg[fp] = {"state": "done", "result": result,
                               "started_at": time.monotonic()}
        return result

    def _run_child(self, child: Agent, agent_type: str, task: str) -> ToolResult:
        """执行 child.run + 提取 digest，映射为 SubAgentResult（C2）。

        asyncio.run 桥接：execute 跑在 to_thread worker 线程（无 running loop），
        必须 asyncio.run 新建事件循环（Layer 3 spec §4.3 钉死，嵌套会抛 RuntimeError）。
        child.run 拿到最终文本后，同一事件循环内 _extract_digest 提取结构化摘要——
        不另开循环：再走一遍 asyncio.run 会丢失本循环状态，且 _run_child_with_budget
        的确认时钟只在本次循环内有效。
        """
        timeout = self._resolve_timeout(agent_type)
        # 用户确认等待不计入执行预算（2026-08-07）：包装 child.confirm_callback 记录等待
        # 时长，_run_child_with_budget 把累积等待加回剩余预算——确认期间子 agent 不被
        # 超时误杀（写盘等用户确认时一直等）。纯执行超时仍正常触发。child 的 confirm
        # 回调是构造时继承的（confirm_callback=parent.confirm_callback），此处只外包计时。
        clock = _UserWaitClock()
        child.confirm_callback = _wrap_confirm_callback(child.confirm_callback, clock)

        async def _run_and_extract():
            # 先跑子 agent（带预算），再对最终文本提取 digest——两段串在同一个
            # asyncio.run 里，digest 提取不消耗 child 的执行预算（独立 30s 超时）。
            text = await _run_child_with_budget(child.run(task), timeout, clock)
            digest = await _extract_digest(self._parent.llm, agent_type, text)
            return text, digest

        try:
            text, digest = asyncio.run(_run_and_extract())
            result = SubAgentResult(status="success", summary=text, digest=digest)
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

            async def _run_and_extract():
                # 与 SpawnSubAgentTool._run_child 同款：child.run + digest 提取串在同一
                # 协程里（此处已在 gather loop 中，直接 await 而非 asyncio.run）。
                text = await _run_child_with_budget(child.run(task), timeout, clock)
                digest = await _extract_digest(parent.llm, agent_type, text)
                return text, digest

            try:
                text, digest = await _run_and_extract()
                return SubAgentResult(status="success", summary=text, digest=digest)
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
