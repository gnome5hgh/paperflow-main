"""共享 spawn 工具层——SpawnSubAgentTool 及配套 helper。

子 agent 派发与结构化结果摘要的实现。装配仍只在 agents/supervisor/
tools.py 的 _make_supervisor_tools——Supervisor 是唯一装配 spawn 工具的 agent
(权限最小化:子 agent 不能递归调度)。需父 agent 注入(needs_parent),见 Tool 约定。
"""
import asyncio
import hashlib
import re
import threading
import time
from typing import Callable

from pydantic import BaseModel

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, StreamEvent
# 运行期状态(去重注册表/失败计数/各类预算计数)统一由容器持有:session 作用域跨 run
# 存活(同会话去重与失败计数),run 作用域按 trace 隔离(派发与预算账本)。容器取用时
# 顺手清扫过期条目,故此处不再单独清理。done 结果可复用窗也复用容器的定义。
from paperflow.core.agent.state import (
    SPAWN_REUSE_WINDOW_S as _SPAWN_REUSE_WINDOW_S, get_run_state, get_session_state)
from paperflow.core.intent.schemas.intent import INTENT_META, IntentType
from paperflow.core.llm import StructuredOutput
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.orchestration.modes import SubAgentMode, SUB_AGENT_MODES


class SubAgentResult(BaseModel):
    """子 agent 的结构化结果,supervisor 据此组织最终回答。

    status ∈ {success, failed, timeout, denied};needs_attention 是独立标志
    (denied + needs_attention=True 表示"被拒且需用户介入",与可重试的 failed 区分)。
    digest 是从子 agent 最终回答提取的结构化摘要,提取失败/超时落 {} (supervisor
    回退读 summary 全文)。
    """
    status: str
    summary: str
    error_detail: str = ""
    needs_attention: bool = False
    digest: dict = {}


class SearcherDigest(BaseModel):
    """searcher 的结果摘要:命中多少篇、有哪些论文、哪些已下载、哪些待确认。

    pending_confirm / needs_attention 对应下载门禁的"待用户确认"路径——这是
    spawn 结果之外的第二处用户介入点,supervisor 需据此提示用户确认。
    """
    count: int
    papers: list[str]
    downloaded: list[str] = []
    pending_confirm: list[str] = []
    needs_attention: bool = False


class ReviewerDigest(BaseModel):
    """reviewer 的结果摘要:裁决结论 + 通过/未通过计数 + 建议下载清单。"""
    verdict: str
    pass_count: int
    fail_count: int
    download_list: list[str] = []


class NoterDigest(BaseModel):
    """noter 的结果摘要:note_path 是产物绝对路径,status 描述写盘结果。"""
    note_path: str = ""
    status: str


class ResearcherDigest(BaseModel):
    """researcher 的结果摘要:四个产物路径 + 状态,supervisor 据此汇报。"""
    status: str
    survey_path: str = ""
    gaps_path: str = ""
    ideas_path: str = ""
    plan_path: str = ""


class LibrarianDigest(BaseModel):
    """librarian 的结果摘要:同步/删除的计数,supervisor 据此汇报。

    status 默认空串:sync_citations 的 tool summary 只有 total/added/skipped/
    rejected、没有 status,LLM 抽 digest 时容易漏该字段——给默认值避免校验失败
    整个 digest 回落为 {},计数一并丢失。
    """
    status: str = ""
    added: int = 0
    skipped: int = 0
    rejected: int = 0
    total: int = 0


class GenericDigest(BaseModel):
    """未注册摘要 schema 的兜底:抽出简短摘要与关键条目,supervisor 不致无从下手。"""
    summary_short: str
    key_items: list[str] = []
    count: int | None = None


def digest_schema_for(agent_type: str) -> type[BaseModel]:
    """按 agent_type 返回对应的摘要 schema;未注册的类型落 GenericDigest。

    spawn 侧按 agent_type 挑 schema,supervisor 按 agent_type 解释 digest——
    新 agent 类型接入只需在此注册。
    """
    return {
        "searcher": SearcherDigest,
        "reviewer": ReviewerDigest,
        "noter": NoterDigest,
        "researcher": ResearcherDigest,
        "librarian": LibrarianDigest,
    }.get(agent_type, GenericDigest)


async def _extract_digest(llm, agent_type: str, text: str,
                          telemetry_callback=None) -> dict:
    """从子 agent 最终回答提取结构化摘要(失败/超时返回空 dict)。

    复用 StructuredOutput 的三层防御(json 模式 + 模型校验 + 重试);独立超时 30s,
    与子 agent 执行超时解耦——摘要提取是"锦上添花",卡死不能拖垮 spawn 主流程。
    只取 text 尾部 2000 字符控制 prompt 长度:子 agent 回答可能很长(如 noter 的
    整篇笔记),结构化摘要只需要结论性尾部。

    :param telemetry_callback: 摘要 LLM 调用的元数据回调,None = 零开销跳过(不接线审计)
    """
    try:
        digest = await asyncio.wait_for(
            StructuredOutput(llm, telemetry_callback=telemetry_callback).extract(
                prompt=f"从以下子 agent 最终回答提取结构化摘要：\n{text[-2000:]}",
                schema=digest_schema_for(agent_type)),
            timeout=30)
        return digest.model_dump()
    except Exception:
        return {}


def _check_spawn_allowed(parent: Agent, agent_type: str) -> str | None:
    """运行时校验父 agent 是否有权 spawn 该子 agent;无权返回错误信息,有权返回 None。

    supervisor 硬编码放行;其余 agent 依据自身 allowed_spawns 白名单校验,越界返回
    错误信息(调用方映射为 denied)。spawn_sub_agent 的运行时校验单点。
    """
    if parent.agent_type == "supervisor":
        return None
    cfg = parent.agent_registry.get_config(parent.agent_type)
    if agent_type not in cfg.allowed_spawns:
        return f"{parent.agent_type} 不能 spawn {agent_type}"
    return None


def _record_dispatch(parent: Agent, agent_type: str, status: str) -> None:
    """把一次派发尝试记入 supervisor 的派发账本（run 状态容器，按 trace 键控）。

    只记 supervisor 自身的派发——子 agent 的内部派发不进这份账本，与每轮派发
    上限的计数口径一致。被拒/去重的尝试也记（状态 denied/deduped），收尾核对时
    模型能据此看到「想派但没派成」的事实。parent 非 supervisor 时直接跳过，不给
    子 agent 的任务留噪声。
    """
    if parent.agent_type != "supervisor":
        return
    get_run_state(parent._trace_id).spawn_dispatches.append((agent_type, status))


#: 并发锁:execute 跑在线程池 worker 里,并行 spawn 会同时读写容器状态——单次
#: dict.get/set 虽原子,但"检查命中-注册 running"两步必须整体原子,否则两线程同时
#: 各自派发一次,去重失效。容器自身的锁只保证建容器与清扫原子,与这里的派发序列锁
#: 各管一段,互不冲突。
_SPAWN_LOCK = threading.Lock()

#: 每次 ReAct 迭代内 supervisor 自身派发的总量上限:去重与信号量只管"是否重复"和
#: "并发几路",不管"一次迭代里总共派了多少路"。没有总量上限时,模型会在一次迭代里
#: 拆出十几路并行检索,token 成本随路数线性放大,且同类子任务过多时汇总质量反而下降。
#: 只统计 supervisor 自身的派发;子 agent 的审稿派发由审稿预算单独封顶,不双重计数。
TURN_SPAWN_BUDGET = 8

#: 失败升级:同会话同 agent_type 连续 N 次非 success(timeout/failed)
#: 后,在结果文本追加强指令「勿再派发,改用 ask_user」——若重试预算用尽仍不升级,
#: 模型会一直自动重试;本项目每次重试是分钟级多工具子任务,故取较紧的 2。仅对
#: supervisor 生效(子 agent 无 ask_user 工具,升级无从谈起);成功即清零,
#: 不按任务文本指纹化。
_FAILURE_ESCALATION_THRESHOLD = 2
_FAILURE_ESCALATION_NOTE = (
    "\n\n⚠️ 该类型子任务已连续 {n} 次失败。请勿再次派发同类型子任务——"
    "改用 ask_user_question 向用户说明失败情况并请示（放弃 / 换思路 / 坚持重试）。"
)

#: 审稿预算门:同一父实例内同类审稿 spawn 的次数上限。值取自旧的「审稿循环最多
#: 3 轮」约定——预算下沉到代码强制后,LLM 不再负责数轮次,超限派发直接拒绝并给出路
#: (基于已有裁决定稿、如实报告未解决项)。计数键 (父实例 id, mode):按「父实例」
#: 而非「父 run」隔离,使同一 run 内共用一个 trace 的多个同类型父实例各算各的、
#: 兄弟不串号;计数存在 run 状态里,跨 run(新 trace)随之重置。不同 mode 独立计数
#: (笔记审稿/下载门禁/计划审稿互不挤占)。仅对真实派发计数——去重命中(running
#: 提示/done 复用)早退在计数之前,不消耗预算。
_REVIEW_SPAWN_MODES = frozenset(m.value for m in (
    SubAgentMode.NOTE_REVIEW, SubAgentMode.DOWNLOAD_REVIEW, SubAgentMode.PLAN_REVIEW))
_REVIEW_SPAWN_BUDGET = 3
_REVIEW_BUDGET_DENIED_NOTE = (
    "同类审稿派发已达预算上限({budget} 次)。请基于已有审查裁决定稿,"
    "并在最终回复中如实报告未解决的 blocking 项,不要再次派发。"
)

#: 任务文本中绝对路径的启发式正则(_task_has_path 的布尔判据):抓 "/" 开头、不含空白/
#: 中文标点/半角逗号分号冒号/引号的最长串。只做「是否含路径」的布尔判断,不读文件。
#: 排除集不含半角括号(如 file(v2).md 能完整识别);中文全角括号仍是分隔符。
#: lookbehind (?<![A-Za-z0-9_]) 让路径前可以是空白/标点(全角冒号/左括号/反引号)或
#: 中文,但不含英文单词字符——这样 Q1/Q2、8/10、a/b 等散文斜杠(前接单词字符)仍忽略,
#: 而「审阅草稿文件：/tmp/x」这类紧邻标点/中文的真路径能识别。误判安全方向:假阳性
#: 保守跳过 done 缓存 → 安全重跑;真路径漏判只剩「前接英文单词字符」这一窄缝。
_PATH_RE = re.compile(r"(?<![A-Za-z0-9_])/[^\s，,;:。（）\"']+")


def _task_fingerprint(task: str, mode: str | None = None) -> str:
    """任务文本指纹 = sha256(规范化空白后的文本 + mode)[:16]。

    mode 参与指纹,防"同任务文本不同模式"的去重碰撞(同 task 但 run 模式不同,
    结果不可互换)。
    """
    norm = " ".join(task.split())
    key = f"{mode or ''}\n{norm}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _task_has_path(task: str) -> bool:
    r"""任务文本是否含绝对路径(布尔判断,不提取):正则命中即 True。

    门控语义:含路径的任务引用真实文件(世界可变——子 agent 执行期间文件可能被改),
    故只做 running 去重、完成即清条目、永不缓存 done;无路径任务(纯文本)才允许
    done 在窗口内复用。误判安全方向:散文里的 "/"(如 "/5 评分")被误判为路径(假阳性)
    → 保守跳过 done 缓存 → 安全重跑,不交付陈旧结果。
    """
    return _PATH_RE.search(task) is not None


def _extract_paths(task: str) -> list[str]:
    """从子任务文本抽出绝对路径（复用 _PATH_RE），用于同路径在途互斥。

    与 _task_has_path 共用同一条启发式正则：同一串文本在「是否含路径」与「含哪些
    路径」两处判定必须一致，否则去重门控与在途互斥会各按一套标准割裂。
    """
    return _PATH_RE.findall(task)


class _UserWaitClock:
    """用户确认等待计时器:确认回调等待期间累积时长,供子 agent 超时预算扣除。

    语义:用户确认是交互等待,不应计入子 agent 的执行预算——预算 = 基础超时 + 已累积
    的用户等待,子 agent 卡在确认上时预算持续延长(一直等用户),纯执行超时仍正常触发。

    begin/end 而非"结束才记":预算循环要看到**进行中**的等待(只记结束时,确认进行中
    total 为 0,预算会误以为没在等用户而误杀)。total() 返回已完成 + 进行中的和。
    确认包装与预算循环在同一事件循环线程,防御性加锁防未来多线程变化。
    """
    def __init__(self) -> None:
        self._completed = 0.0
        self._active_start: float | None = None   # 确认进行中的 monotonic 起点
        self._lock = threading.Lock()

    def begin(self) -> None:
        """确认等待开始(进入确认回调前调用)。"""
        with self._lock:
            if self._active_start is None:
                self._active_start = time.monotonic()

    def end(self) -> None:
        """确认等待结束(finally 里调用)——把进行中时长并入已完成。"""
        with self._lock:
            if self._active_start is not None:
                self._completed += time.monotonic() - self._active_start
                self._active_start = None

    def total(self) -> float:
        """当前总用户等待 = 已完成 + 进行中(预算循环每轮据此重算剩余预算)。"""
        with self._lock:
            active = (time.monotonic() - self._active_start
                      if self._active_start is not None else 0.0)
            return self._completed + active


def _wrap_confirm_callback(orig, clock: _UserWaitClock):
    """包装确认回调:外包计时,把等待时长记入 clock(预算据此延长)。

    原回调(如 CLI 的 stdin 确认)语义不变——只加 begin/end 计时。finally 保证无论
    确认/拒绝/异常都停止计时,不把用户等待泄漏到后续工具的执行预算。
    """
    async def wrapped(cr):
        clock.begin()
        try:
            return await orig(cr)
        finally:
            clock.end()
    return wrapped


def _wrap_ask_user_callback(orig, clock: _UserWaitClock):
    """包装问用户回调(同步契约):同款 begin/end 计时,把用户思考时间记入 clock。

    ask_user_callback 是同步 Callable[[str], str](AskUserQuestionTool 在线程池里
    直接调用,与 async 的 confirm_callback 契约不同,故单独一个同步包装)。语义与
    confirm 版一致:用户思考/输入是交互等待,不计入子 agent 执行预算——实测一次
    ask_user 的用户回答耗时 117.9s,不排除会吃掉预算的 13%。
    """
    def wrapped(question):
        clock.begin()
        try:
            return orig(question)
        finally:
            clock.end()
    return wrapped


async def _run_child_with_budget(coro, timeout: float, clock: _UserWaitClock):
    """运行子 agent 协程,预算 = 基础超时 + 用户确认等待累积(确认期间超时不暂停)。

    替代 asyncio.wait_for 的纯墙钟语义:用户忘确认时,wait_for 会吃掉预算把任务误杀。
    本函数每轮重算剩余 = (基础截止时间 + 累积用户等待) - 当前时刻,剩余 ≤0 才取消并
    抛 asyncio.TimeoutError。子 agent 卡在确认上时 clock.total() 持续增长 → 剩余为正
    → 一直等用户;纯执行超时(无等待兜底)仍正常触发。

    实现用 asyncio.wait({task}, timeout) 轮询:超时一轮只是本轮 wait 到期,任务继续
    运行未取消;下一轮重算剩余再等。任务完成则返回其结果(异常原样上抛,如
    MaxTurnsExceeded 由调用方映射为 failed)。

    取消级联(真实使用测试 P0-2):父任务被取消(Ctrl+C)时,把取消传播给子任务并等它
    收尾后再抛——aexecute 在父事件循环上直接 await 本协程,级联取消即整棵 agent 树
    一起终止,不再留孤儿子 agent 继续跑、烧 token。
    """
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)
    base_deadline = loop.time() + timeout
    try:
        while not task.done():
            remaining = (base_deadline + clock.total()) - loop.time()
            if remaining <= 0:
                # 纯执行超时(无用户等待兜底)→ 取消子 agent,抛 TimeoutError
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
    except asyncio.CancelledError:
        task.cancel()
        try:
            await task
        except BaseException:
            pass
        raise


def _make_child_stream_callback(parent) -> Callable[[StreamEvent], None] | None:
    """构造子 agent 的流式回调：不流 content，结构化 tool 事件透传（前缀由渲染器统一加）。

    子 agent 推理内容不向终端流式输出（多路并发会串字），只透传结构化工具事件
    （tool_start/tool_end）；渲染层按 ev.agent_type 统一加 [{agent}] 前缀
    （root 也带 supervisor）。父无 stream_callback（非 CLI 调用方）时返回
    None——子 agent 零流式，零开销。
    """
    pcb = getattr(parent, "stream_callback", None)
    if pcb is None:
        return None

    def child_cb(ev: StreamEvent) -> None:
        if ev.kind in ("tool_start", "tool_end"):
            pcb(ev)          # 前缀由渲染器统一加，此处不再拼 agent_type
    return child_cb


class SpawnSubAgentTool(Tool):
    """派发单个子 agent,返回 SubAgentResult 的序列化结果。"""

    name = "spawn_sub_agent"
    description = ("派发单个 SubAgent 执行子任务，返回结构化结果（status/summary/error_detail/"
                   "digest/needs_attention），digest 为子任务的结构化摘要（提取失败时为空）。"
                   "失败可依据 error_detail 决定重试或上报。")
    parameters = {
        "type": "object",
        "properties": {
            "agent_type": {"type": "string", "description": "目标 SubAgent 类型，如 searcher"},
            "task": {"type": "string", "description": "子任务文本（含实体，已拼入上下文）"},
            "mode": {"type": "string",
                     "enum": [m.value for m in SubAgentMode],
                     "description": "子 agent 运行模式(可选)。noter: note;"
                                    "reviewer: note_review/download_review;"
                                    "不传 = 子 agent 默认模式"},
            "intent": {"type": "string",
                       "enum": [t.value for t in IntentType],
                       "description": "本次派发服务的意图（可选）。会话意图被误判时，"
                                      "显式声明可覆盖判定放行；亦可作为审计标注。"
                                      "顺序与并行由你自己决定，框架不做限制。"},
        },
        "required": ["agent_type", "task"],
    }
    #: 需要父 Agent 引用(构造时只注入声明者)
    needs_parent = True
    risk_level = "low"
    #: 异步工具：Agent 在父事件循环上直接 await aexecute——子 agent 与父同循环，
    #: 父被取消（Ctrl+C）时 CancelledError 沿 await 链传播进子 agent，整棵任务树
    #: 级联终止（真实使用测试 P0-2 的根治）。execute 保留为同步兼容路径
    #: （asyncio.run 包装，供测试/无循环上下文调用）。
    async_execute = True
    #: 子 agent 超时秒数的类默认(config 的 agent_timeouts 命中时被覆盖)。
    #: 保留为类属性:既是无配置时的兜底,也是测试覆盖点(测例可设极小值验证超时路径)。
    timeout = 120

    def __init__(self, agent_timeouts: dict[str, int] | None = None):
        # 按 agent 类型的超时覆盖表从 config 注入;无表(如测试直接构造)时
        # 回退到类属性 timeout,既有测例不被破坏。
        self._agent_timeouts = agent_timeouts or {}

    def _resolve_timeout(self, agent_type: str) -> int:
        """解析该 agent 生效超时:配置命中优先,否则类默认。"""
        return self._agent_timeouts.get(agent_type, self.timeout)

    def execute(self, agent_type: str, task: str, mode: str | None = None,
                intent: str | None = None) -> ToolResult:
        """同步兼容路径：在调用方线程新建事件循环跑 aexecute。

        Agent 执行器对 async_execute 工具走 aexecute（父循环 await，级联取消）；
        本方法仅供测试/无事件循环上下文直接调用。
        """
        return asyncio.run(self.aexecute(agent_type, task, mode, intent))

    def _admit(self, agent_type: str, task: str, mode: str | None = None,
               intent: str | None = None) -> "ToolResult | tuple[str, bool]":
        """派发前的七道闸，按判定顺序：mode 校验 → 意图派发门禁 → spawn 白名单
        → 同会话同指纹去重 → 审稿预算 → 每轮派发总量上限 → 同路径在途互斥。

        意图只作信号，不强制派发顺序——顺序与并行由 supervisor 自主决定。每条
        被拒/去重的派发尝试都记入 supervisor 的派发账本（状态 denied/deduped），
        供收尾核对看到「想派但没派成」的事实。

        通过时返回 (任务指纹, 是否含路径)——调用方负责在执行完的 finally 里
        按 has_path 决定 done 缓存或清条目；拒绝时直接返回 denied/去重命中的
        ToolResult。审稿类 mode 的预算计数与注册同锁原子,拒绝路径不触碰注册表。
        """
        parent = self._parent
        # mode 参数校验：非法值直接拒绝（schema enum 约束 LLM 生成层，
        # 此处兜底防任何漏网之鱼静默错流——拼写错的 mode 注入会让子 agent 走错流程）。
        if mode is not None and mode not in SUB_AGENT_MODES:
            _record_dispatch(parent, agent_type, "denied")
            result = SubAgentResult(status="denied",
                                    summary=f"未知 mode: {mode}，合法值: {sorted(SUB_AGENT_MODES)}")
            return ToolResult(text=result.model_dump_json(), summary=result.model_dump())
        # 意图派发门禁：代码级确定性检查，不依赖 supervisor 遵循 AGENT.md 提示词。
        #
        # 声明优先：supervisor 显式声明了 intent 时按声明校验——可派发即放行，
        # 不可派发明确拒绝。这是会话意图被误判时唯一的申诉通道：用户已在澄清中
        # 确认真实意图、而 last_intent 要到下一轮才更新，只认 last_intent 会把
        # 「模型+用户都确认正确」的派发也锁死。安全性与原设计一致——intent 是
        # 自声明，报假声明换不到任何额外权限，声明什么就按什么校验。
        # 未声明时看本轮会话意图：last_intent 是 dispatch_allowed=False 的意图
        # （陈述方向/系统类）就拒绝派发领域 agent；last_intent 为 None（意图管线
        # 失败降级）时放行，不因门禁误伤主流程。
        declared: IntentType | None = None
        if intent is not None:
            try:
                declared = IntentType(intent)
            except ValueError:
                _record_dispatch(parent, agent_type, "denied")
                result = SubAgentResult(
                    status="denied",
                    summary=f"未知 intent: {intent}，合法值为 IntentType 枚举")
                return ToolResult(text=result.model_dump_json(), summary=result.model_dump())
        if declared is not None:
            if not INTENT_META[declared][1]:
                _record_dispatch(parent, agent_type, "denied")
                result = SubAgentResult(
                    status="denied",
                    summary=f"声明的意图 {declared.value} 不可派发领域 agent（仅业务意图可派发）")
                return ToolResult(text=result.model_dump_json(), summary=result.model_dump())
        else:
            li = parent.last_intent
            if li is not None and not INTENT_META[li.intent_type][1]:
                _record_dispatch(parent, agent_type, "denied")
                result = SubAgentResult(status="denied",
                                        summary=f"当前意图 {li.intent_type.value} 不派发领域 agent")
                return ToolResult(text=result.model_dump_json(), summary=result.model_dump())
        # ① spawn 权限运行时校验(_check_spawn_allowed 单点)。
        #    supervisor 硬编码放行;非 supervisor 越界 spawn → denied。
        denied = _check_spawn_allowed(parent, agent_type)
        if denied is not None:
            _record_dispatch(parent, agent_type, "denied")
            result = SubAgentResult(status="denied", summary=denied)
            return ToolResult(text=result.model_dump_json(), summary=result.model_dump())

        # ② 同会话同指纹去重(机械安全网):并行 spawn 并发访问注册表,检查+注册
        #    须持锁整体原子。门控规则由 _task_has_path 区分:
        #    - 无路径任务(纯文本,世界不变)→ running 提示 + done 窗口内缓存复用
        #    - 有路径任务(引用真实文件,世界可变)→ 只 running 去重,完成即清条目、
        #      永不缓存 done——子 agent 执行期间文件可能已改,缓存旧结果会交付陈旧裁决
        #    去重注册表在会话容器上(跨 run 存活,同会话内生效),下面用到的各类预算
        #    计数在 run 容器上(按 trace 隔离,一次用户任务内独立)。取容器时其内部会
        #    顺手剔除过窗条目(超窗 done 缓存、闲置过久的整份 run 状态),长会话不会
        #    无限累积,故此处不再单独清理。
        sess = get_session_state(parent.session_id)
        rs = get_run_state(parent._trace_id)
        fp = _task_fingerprint(task, mode)
        has_path = _task_has_path(task)
        with _SPAWN_LOCK:
            reg = sess.spawn_registry
            hit = reg.get(fp)
            now = time.monotonic()
            if hit and hit["state"] == "running":
                # 去重命中：同指纹任务正在跑，提示等待。记账为 deduped——
                # 收尾核对据此看出「这一路想派但被去重早退了」。
                _record_dispatch(parent, agent_type, "deduped")
                return ToolResult(text="同任务正在执行中，请等待其结果（已去重，勿重复派发）")
            if hit and hit["state"] == "done" and not has_path \
                    and now - hit["started_at"] < _SPAWN_REUSE_WINDOW_S:
                # done 缓存复用：同任务刚做完、结果直接给你。同样记 deduped。
                _record_dispatch(parent, agent_type, "deduped")
                return hit["result"]
            # ③ 审稿预算门:审稿类 mode 在注册 running 前计数检查——超限拒绝(不注册,
            #    不污染去重注册表);去重命中早退不计数。置于注册前是 _admit 的既有
            #    不变式:所有 ToolResult 返回都发生在注册 running 之前,否则异常路径
            #    会留下永久 running 条目堵塞同指纹后续派发。键用父实例 id:同一 run
            #    内多个同类型父实例(如两个 noter)各算各的,不因共用同一 trace 串号;
            #    计数随 run 状态存活,跨 run(新 trace)自然重置。
            if mode in _REVIEW_SPAWN_MODES:
                bkey = (parent._instance_id, mode)
                used = rs.review_counts.get(bkey, 0)
                if used >= _REVIEW_SPAWN_BUDGET:
                    _record_dispatch(parent, agent_type, "denied")
                    denied_result = SubAgentResult(
                        status="denied",
                        summary=_REVIEW_BUDGET_DENIED_NOTE.format(budget=_REVIEW_SPAWN_BUDGET))
                    return ToolResult(text=denied_result.model_dump_json(),
                                      summary=denied_result.model_dump())
                rs.review_counts[bkey] = used + 1
            # ④ 每轮派发总量上限:只统计 supervisor 自身的派发,按 ReAct 迭代下标
            #    (_current_turn,每轮 LLM 迭代自增)计数——封顶的是每次迭代内 supervisor
            #    能并行派发多少路,下一次迭代即重新起算,不会因为上一次迭代派得多而
            #    永久锁死。审稿预算拒绝在前,不消耗本次迭代的额度。
            if parent.agent_type == "supervisor":
                turn = getattr(parent, "_current_turn", 0)
                used = rs.turn_spawn_counts.get(turn, 0)
                if used >= TURN_SPAWN_BUDGET:
                    _record_dispatch(parent, agent_type, "denied")
                    denied_result = SubAgentResult(
                        status="denied",
                        summary=f"本轮派发已达上限 {TURN_SPAWN_BUDGET}，"
                                "请先汇总已有结果向用户交代，需要继续时下一轮再派。")
                    return ToolResult(text=denied_result.model_dump_json(),
                                      summary=denied_result.model_dump())
                rs.turn_spawn_counts[turn] = used + 1
            # ⑤ 同路径在途互斥:两个任务文本可以完全不同(去重指纹不碰撞),却写同一个
            #    目标文件——并发跑就会静默互相覆盖(原子写只防撕裂不防覆盖)。把任务
            #    文本里抽出的绝对路径与「同一父实例」正在写的路径集比对,命中即拒。
            #    只按父实例分桶,不按 trace 全局分桶:真正会同时写同一文件的,是同一个
            #    父在同一轮里扇出的多路(兄弟 spawn);祖先任务文本里提到某路径不代表
            #    后代要写它(后代或只读,或顺序依赖父产物),按父实例分桶才不会把 noter
            #    写完再内部 spawn reviewer 审稿这类顺序流程误判成并发写。
            #    路径检查置于所有既有拒绝分支之后:被别的闸拒绝的派发不会留下已占用的
            #    路径——若前移到预算分支之前,一次被别的闸拒绝的派发会先占用路径再早退
            #    (早退不进 aexecute 的 finally),该路径就被永久锁死。代价是本闸的拒绝
            #    发生在审稿/每轮预算自增之后,会照常消耗一次对应额度——这是刻意的取舍。
            #    只拦「同时在途」,不拦「按序重写已完成 spawn 写过的文件」——重新生成
            #    笔记是合法行为。
            target_paths = _extract_paths(task)
            occupied = rs.in_flight_paths.get(parent._instance_id, set())
            clash = [p for p in target_paths if p in occupied]
            if clash:
                _record_dispatch(parent, agent_type, "denied")
                denied_result = SubAgentResult(
                    status="denied",
                    summary=f"目标路径在途占用，正在被另一个子任务写：{'、'.join(clash)}。"
                            "请先等它完成，或改为写不同的文件。")
                return ToolResult(text=denied_result.model_dump_json(),
                                  summary=denied_result.model_dump())
            if target_paths:
                rs.in_flight_paths.setdefault(parent._instance_id, set()).update(target_paths)
            reg[fp] = {"state": "running", "result": None, "started_at": now}
        # 全部检查通过、任务已注册 running（拒绝路径都在上方提前 return）。
        return fp, has_path

    async def aexecute(self, agent_type: str, task: str, mode: str | None = None,
                       intent: str | None = None) -> ToolResult:
        """派发一个子 agent（父事件循环上 await），返回 SubAgentResult 序列化结果。

        与同步路径同一套门禁与去重；子 agent 与父同循环——取消级联、流式事件、
        审计归属全部天然对齐，不再经工作线程 + 独立事件循环。
        """
        admitted = self._admit(agent_type, task, mode, intent)
        if isinstance(admitted, ToolResult):
            return admitted
        fp, has_path = admitted
        # 派发序列已过闸，此后收尾要写回去重注册表与失败计数——容器按作用域取用，
        # 与 _admit 里的局部变量无关（这是另一个方法）。
        parent = self._parent
        sess = get_session_state(parent.session_id)
        # run 容器：收尾在 finally 里释放本次派发占用的目标路径（_admit 已登记在它上面）
        rs = get_run_state(parent._trace_id)

        result = None
        try:
            # ③ 构造子 agent:继承父的安全中间件、会话 ID(同一审计链)、确认回调与
            #    问用户回调——确认回调是关键:noter 的写盘工具要求用户确认,不传则
            #    默认回调始终拒绝,spawn 出的 noter 永远写不出笔记;问用户回调同理,
            #    noter/qa-agent 靠它中途向用户提问。不传意图管线/会话 → 子 agent 不做
            #    意图识别(子任务是结构化任务,非用户意图)。
            # 流式统一：子 agent 只透传工具行（前缀由渲染器统一加）、不流 content——
            # 与并行场景同一代码路径（多路并发不串字）。
            child = Agent(
                llm=parent.llm, agent_registry=parent.agent_registry,
                skill_registry=getattr(parent, "skill_registry", None),
                agent_type=agent_type, security_middleware=parent.security_middleware,
                session_id=parent.session_id, confirm_callback=parent.confirm_callback,
                ask_user_callback=parent.ask_user_callback,
                stream_callback=_make_child_stream_callback(parent),
                # 继承父 trace_id：去重池（get_run_state 按 trace_id 键控）在
                # 一次用户任务内跨 agent 共享——子 agent 因此不重复下载/抓取父任务
                # 已处理过的资源（父超时重试时会派出新的 searcher，不共享池就会重抓）。
                trace_id=getattr(parent, "_trace_id", None),
            )
            if mode:
                child.system_prompt = f"当前模式：{mode}\n{child.system_prompt}"
            # 传解析后的超时:_run_child 用实际生效值(config > 类默认)
            result = await self._run_child(child, agent_type, task)
            # 派发账本：真实派发完成后按结果状态记账（与早退路径的 denied/deduped
            # 互补），供收尾核对列出「本轮派了哪些、结果如何」。
            _record_dispatch(parent, agent_type, result.summary.get("status", ""))
            # 失败升级：仅 supervisor 的派发计数——连续 N 次非 success
            # 后追加强指令，把「继续自动重试」的决策权交回用户（模型对不可能
            # 成功的任务会自动重派多轮，每轮分钟级）。按会话容器计数：同一会话
            # 内跨 run 累计，成功即清零，换 agent_type 各算各的。
            if parent.agent_type == "supervisor":
                if result.summary.get("status") == "success":
                    sess.failure_counts.pop(agent_type, None)
                    sess.failure_counts_at.pop(agent_type, None)
                else:
                    n = sess.failure_counts.get(agent_type, 0) + 1
                    sess.failure_counts[agent_type] = n
                    sess.failure_counts_at[agent_type] = time.monotonic()
                    if n >= _FAILURE_ESCALATION_THRESHOLD:
                        result = ToolResult(
                            text=result.text + _FAILURE_ESCALATION_NOTE.format(n=n),
                            summary=result.summary)
        finally:
            # 完成收尾:无路径写 done 供窗口内复用;有路径/异常 → 清条目不缓存
            # (有路径任务世界可变永不缓存 done;result 为 None 表示构造/执行异常,
            # 防 None 入缓存污染后续复用)。注册表读写全在锁内。
            with _SPAWN_LOCK:
                reg = sess.spawn_registry
                if result is None or has_path:
                    reg.pop(fp, None)
                else:
                    reg[fp] = {"state": "done", "result": result,
                               "started_at": time.monotonic()}
                # 释放本次派发占用的目标路径(与注册表清理同处、同锁):任务已结束,
                # 同路径的新派发送下一轮即可放行。只摘本父实例名下的这些路径,别的
                # 父实例即便占用同一路径也不受影响(各自分桶)。
                owned = rs.in_flight_paths.get(parent._instance_id)
                if owned is not None:
                    owned.difference_update(_extract_paths(task))
                    if not owned:
                        rs.in_flight_paths.pop(parent._instance_id, None)
        return result

    async def _run_child(self, child: Agent, agent_type: str, task: str) -> ToolResult:
        """在父事件循环上执行子 agent.run + 提取摘要,映射为 SubAgentResult。

        取消语义:父任务被取消时 CancelledError 沿 await 链传入
        _run_child_with_budget（内部把取消传播给子任务）,再原样上抛——
        _exec_tool 不捕 BaseException,gather 与 run() 的历史自愈随后接力。
        """
        timeout = self._resolve_timeout(agent_type)
        # 用户确认等待不计入执行预算:包装子 agent 的确认回调记录等待时长,
        # _run_child_with_budget 把累积等待加回剩余预算——写盘等用户确认时一直等,
        # 不被超时误杀;纯执行超时仍正常触发。子 agent 的确认回调是构造时继承父的,
        # 此处只外包计时。
        clock = _UserWaitClock()
        child.confirm_callback = _wrap_confirm_callback(child.confirm_callback, clock)
        # 问用户回调同款计时:用户思考/输入也是交互等待,不计入执行预算
        # (callback 可为 None——程序化/测试环境无交互,零开销跳过)。
        if child.ask_user_callback is not None:
            child.ask_user_callback = _wrap_ask_user_callback(child.ask_user_callback, clock)

        async def _run_and_extract():
            # 先跑子 agent(带预算),再对最终文本提取摘要——两段串在同一事件循环里,
            # 摘要提取不消耗子 agent 的执行预算(独立 30s 超时)。
            # 摘要 LLM 调用归属父:父在做摘要提取,归父的 trace/当前轮次;getattr
            # 兜底防父为 mock/旧对象时读属性崩溃。
            text = await _run_child_with_budget(child.run(task), timeout, clock)
            digest = await _extract_digest(
                self._parent.llm, agent_type, text,
                telemetry_callback=lambda data: self._parent._emit_llm_call(
                    getattr(self._parent, "_current_turn", 0), data))
            return text, digest

        try:
            text, digest = await _run_and_extract()
            result = SubAgentResult(status="success", summary=text, digest=digest)
        except asyncio.TimeoutError:
            result = SubAgentResult(status="timeout", summary="子任务执行超时",
                                    # 插值解析后的超时(配置命中时非类默认),报错可行动
                                    error_detail=f"SubAgent 在 {timeout}s 内未完成")
        except PermissionError as e:
            # 防御性分支:当前架构子 agent 的执行器把策略拒绝/安全拦截降级为普通文本,
            # 不向上抛,几乎不会触发。保留此分支对齐失败处理,不据此推导真实路径。
            result = SubAgentResult(status="denied", summary="子任务被策略引擎拒绝",
                                    error_detail=str(e), needs_attention=True)
        except Exception as e:
            result = SubAgentResult(status="failed", summary="子任务执行失败",
                                    error_detail=str(e))
        return ToolResult(text=result.model_dump_json(), summary=result.model_dump())
