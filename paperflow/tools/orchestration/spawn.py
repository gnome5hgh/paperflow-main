"""共享 spawn 工具层——SpawnSubAgentTool 及配套 helper。

子 agent 派发与结构化结果摘要的实现。装配给 supervisor（硬编码放行所有子 agent）
与需要内部审稿/补料的 paper-agent/note-agent/research-agent（按各自 allowed_spawns 白名单）；
叶子 agent（review-agent/citation-agent/memory-agent）不装配、不递归调度。需父 agent 注入
(needs_parent),见 Tool 约定。

派发前 _admit 的五道闸（未知类型 / spawn 白名单 /
同指纹去重（同一批内的机械重复） / 审稿预算 / 每轮派发上限）与闸门状态容器
（session/run 两作用域,见 core/agent/state.py）都在本模块;顺序与并行由父
agent 自主决定,框架不强制。
"""
import asyncio
import hashlib
import threading
import time
from typing import Callable

from pydantic import BaseModel

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, StreamEvent
# 运行期状态由容器持有:去重注册表与各类预算计数在 run 作用域(按 trace 隔离),
# 失败计数在会话作用域(跨 run 累计)。容器取用时顺手清扫过期条目,故此处不再单独清理。
from paperflow.core.agent import get_run_state, get_session_state
from paperflow.core.llm import StructuredOutput
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.orchestration.constants import (
    SubAgentStatus)


class SubAgentResult(BaseModel):
    """子 agent 的结构化结果,supervisor 据此组织最终回答。

    status ∈ SubAgentStatus 的「子任务结果」子集;needs_attention 是独立标志
    (denied + needs_attention=True 表示"被拒且需用户介入",与可重试的 failed 区分)。
    digest 是从子 agent 最终回答提取的结构化摘要,提取失败/超时落 {} (supervisor
    回退读 summary 全文)。

    Attributes:
        status: SubAgentStatus，子任务结果（success | failed | timeout | denied）
        summary: str，子 agent 的最终回答全文
        error_detail: str，失败/超时/拒绝的细节（成功为空）
        needs_attention: bool，被拒且需用户介入（与可重试的 failed 区分）
        digest: dict，结构化摘要；提取失败/超时为 {}（supervisor 回退读 summary 全文）
    """
    status: SubAgentStatus
    summary: str
    error_detail: str = ""
    needs_attention: bool = False
    digest: dict = {}


class PaperAgentDigest(BaseModel):
    """paper-agent 的结果摘要:命中多少篇、有哪些论文、哪些已下载、哪些待确认。

    pending_confirm / needs_attention 对应下载门禁的"待用户确认"路径——这是
    spawn 结果之外的第二处用户介入点,supervisor 需据此提示用户确认。

    Attributes:
        count: int，命中篇数
        papers: list[str]，命中的论文标题
        downloaded: list[str]，已下载的论文
        pending_confirm: list[str]，待用户确认的下载项
        needs_attention: bool，是否存在需用户介入的项（如下载门禁待确认）
        points: list[str]，读类任务的材料要点（检索路留空）
        provenance: list[str]，读类任务的片段出处（原文路径 + 章节/页码；检索路留空）
    """
    count: int = 0
    papers: list[str] = []
    downloaded: list[str] = []
    pending_confirm: list[str] = []
    needs_attention: bool = False
    #: 读类任务（读整篇 / 图表问题）的材料要点；检索路留空
    points: list[str] = []
    #: 读类任务的片段出处（原文路径 + 章节/页码）；检索路留空
    provenance: list[str] = []


class ReviewAgentDigest(BaseModel):
    """review-agent 的结果摘要:裁决结论 + 通过/未通过计数 + 建议下载清单。

    Attributes:
        verdict: str，裁决结论（pass/fail）
        pass_count: int，通过条目数
        fail_count: int，未通过条目数
        download_list: list[str]，建议下载清单
    """
    verdict: str
    pass_count: int
    fail_count: int
    download_list: list[str] = []


class NoteAgentDigest(BaseModel):
    """note-agent 的结果摘要:note_path 是产物绝对路径,status 描述写盘结果。

    Attributes:
        note_path: str，笔记产物的绝对路径
        status: str，写盘结果
    """
    note_path: str = ""
    status: str


class ResearchAgentDigest(BaseModel):
    """research-agent 的结果摘要:四个产物路径 + 状态,supervisor 据此汇报。

    Attributes:
        status: str，研究链路的结果状态
        survey_path: str，survey 产物路径
        gaps_path: str，gaps 产物路径
        ideas_path: str，idea 卡路径
        plan_path: str，研究计划路径
    """
    status: str
    survey_path: str = ""
    gaps_path: str = ""
    ideas_path: str = ""
    plan_path: str = ""


class CitationAgentDigest(BaseModel):
    """citation-agent 的结果摘要:同步/删除的计数 + 被拒条目与原因,supervisor 据此汇报。

    status 默认空串:sync_citations 的 tool summary 只有 total/added/skipped/
    rejected、没有 status,LLM 抽 digest 时容易漏该字段——给默认值避免校验失败
    整个 digest 回落为 {},计数一并丢失。rejected_items/blocked_reason 是给上级的
    可行动线索:知道是哪几篇、为什么被拒,才能决定补什么料、派谁去补。

    Attributes:
        status: str，操作状态（默认空串——sync 的 summary 无该字段，给默认避免整份 digest 回落为 {}）
        added: int，新增条目数
        skipped: int，已在库跳过数
        rejected: int，拒绝入库数
        total: int，扫描总数
        rejected_items: list[str]，被拒条目的可辨识名
        blocked_reason: str，被拒的原因类别
    """
    status: str = ""
    added: int = 0
    skipped: int = 0
    rejected: int = 0
    total: int = 0
    #: 被拒条目的可辨识名(标题或路径),让上级知道该补哪几篇
    rejected_items: list[str] = []
    #: 被拒的原因类别(缺元数据 / 解析失败 / 不在语料 …),让上级知道该派谁补
    blocked_reason: str = ""


class RagAgentDigest(BaseModel):
    """rag-agent 的结果摘要：入库/跳过/清理篇数与失败清单。

    Attributes:
        indexed: int，成功入库的篇数
        skipped: int，未入库的篇数（路径不在语料根下、文件不存在、切块后无内容）
        removed: int，清理掉的已删除文档篇数
        chunks: int，本次写入的块数合计
        failed: list[str]，失败的文件路径
    """
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    chunks: int = 0
    failed: list[str] = []


class GenericDigest(BaseModel):
    """未注册摘要 schema 的兜底:抽出简短摘要与关键条目,supervisor 不致无从下手。

    Attributes:
        summary_short: str，简短摘要
        key_items: list[str]，关键条目
        count: int | None，条目数（不适用时为 None）
    """
    summary_short: str
    key_items: list[str] = []
    count: int | None = None


def digest_schema_for(agent_type: str) -> type[BaseModel]:
    """按 agent_type 返回对应的摘要 schema;未注册的类型落 GenericDigest。

    spawn 侧按 agent_type 挑 schema,supervisor 按 agent_type 解释 digest——
    新 agent 类型接入只需在此注册。

    Args:
        agent_type: str，子 agent 类型

    Returns:
        对应的摘要 pydantic 模型类；未注册的类型返回 GenericDigest。
    """
    return {
        "paper-agent": PaperAgentDigest,
        "review-agent": ReviewAgentDigest,
        "note-agent": NoteAgentDigest,
        "research-agent": ResearchAgentDigest,
        "citation-agent": CitationAgentDigest,
        "rag-agent": RagAgentDigest,
    }.get(agent_type, GenericDigest)


async def _extract_digest(llm, agent_type: str, text: str,
                          telemetry_callback=None) -> dict:
    """从子 agent 最终回答提取结构化摘要(失败/超时返回空 dict)。

    复用 StructuredOutput 的三层防御(json 模式 + 模型校验 + 重试);独立超时 30s,
    与子 agent 执行超时解耦——摘要提取是"锦上添花",卡死不能拖垮 spawn 主流程。
    只取 text 尾部 2000 字符控制 prompt 长度:子 agent 回答可能很长(如 note-agent 的
    整篇笔记),结构化摘要只需要结论性尾部。

    Args:
        telemetry_callback: 摘要 LLM 调用的元数据回调,None = 零开销跳过(不接线审计)

    """
    # 摘要提取是可选增强,因此这里故意吞掉所有异常(超时、JSON 解析失败、schema 校验不过)
    # 并返回空 dict,让调用方回退读 summary 全文——若让异常逃逸,一次摘要失败就会把一个
    # 已经跑成功的子任务误判成 spawn 失败,把"锦上添花"的环节变成新的失败面。
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

    Args:
        parent: Agent，发起派发的父实例
        agent_type: str，目标子 agent 类型

    Returns:
        有权返回 None；越界返回错误信息（调用方映射为 denied）。
    """
    if parent.agent_type == "supervisor":
        return None
    cfg = parent.agent_registry.get_config(parent.agent_type)
    if agent_type not in cfg.allowed_spawns:
        return f"{parent.agent_type} 不能 spawn {agent_type}"
    return None


def _deny(summary: str) -> ToolResult:
    """构造一次派发拒绝的结果。

    Args:
        summary: str，给模型看的拒绝原因——需可行动(说清为什么被拒、该怎么调整)

    Returns:
        ToolResult，text 与 summary 均为同一份 SubAgentResult 的 JSON 序列化。
    """
    result = SubAgentResult(status=SubAgentStatus.DENIED, summary=summary)
    return ToolResult(text=result.model_dump_json(), summary=result.model_dump())


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

#: 失败升级:同会话同 agent_type 连续 N 次非 success(timeout/failed)后,
#: 在结果文本追加强指令「勿再派发,改用向用户请示」——若重试预算用尽仍不升级,模型会一直自动重试;
#: 本项目每次重试是分钟级多工具子任务,故取较紧的 2。
#: 仅对该派发的父 agent 是 supervisor 时生效（下面按 parent.agent_type 代码级判定）
#: ——升级的语义是把「要不要继续重试」的决策权交回用户，只有对用户负责的编排层适用；
#: 成功即清零,不按任务文本指纹化。
_FAILURE_ESCALATION_THRESHOLD = 2
_FAILURE_ESCALATION_NOTE = (
    "\n\n⚠️ 该类型子任务已连续 {n} 次失败。请勿再次派发同类型子任务——"
    "改用向用户说明失败情况并请示（把问题写进你的回答，本轮就此结束；放弃 / 换思路 / 坚持重试）。"
)

#: 审稿预算门:同一父实例内同类审稿 spawn 的次数上限。值取自旧的「审稿循环最多3 轮」约定——
#: 预算下沉到代码强制后,LLM 不再负责数轮次,超限派发直接拒绝并给出路(基于已有裁决定稿、如实报告未解决项)。
#: 计数键 (父实例 id, 目标类型):按「父实例」而非「父 run」隔离,
#: 使同一 run 内共用一个 trace 的多个同类型父实例各算各的、兄弟不串号;
#: 计数存在 run 状态里,跨 run(新 trace)随之重置。
#: 同一父实例派 review-agent 的 3 次预算独立计数。仅对真实派发计数——
#: 去重命中(running 提示)早退在计数之前,不消耗预算。
_REVIEW_SPAWN_BUDGET = 3
_REVIEW_BUDGET_DENIED_NOTE = (
    "同类审稿派发已达预算上限({budget} 次)。请基于已有审查裁决定稿,"
    "并在最终回复中如实报告未解决的 blocking 项,不要再次派发。"
)


def _task_fingerprint(task: str) -> str:
    """任务文本指纹 = sha256(规范化空白后的文本)[:16]。


    Args:
        task: str，子任务文本

    Returns:
        sha256(规范化文本) 的前 16 位十六进制指纹。
    """
    # 先折叠空白再哈希:同一任务只差换行或多空格时应命中同一条目,否则模型把任务文本
    # 换个排版就绕过去重、重复派发同一个子任务。
    norm = " ".join(task.split())
    key = norm
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


class _UserWaitClock:
    """用户确认等待计时器:确认回调等待期间累积时长,供子 agent 超时预算扣除。

    语义:用户确认是交互等待,不应计入子 agent 的执行预算——预算 = 基础超时 + 已累积
    的用户等待,子 agent 卡在确认上时预算持续延长(一直等用户),纯执行超时仍正常触发。

    begin/end 而非"结束才记":预算循环要看到**进行中**的等待(只记结束时,确认进行中
    total 为 0,预算会误以为没在等用户而误杀)。total() 返回已完成 + 进行中的和。
    确认包装与预算循环在同一事件循环线程,防御性加锁防未来多线程变化。

    Attributes:
        _completed: float，已完成的确认等待累计时长（秒）
        _active_start: float | None，进行中的确认等待起点
        _lock: threading.Lock，防御性加锁（当前调用方均在同一事件循环线程）
    """
    def __init__(self) -> None:
        """初始化空计时器（无已完成等待、无进行中的等待）。"""
        self._completed = 0.0
        self._active_start: float | None = None   # 确认进行中的 monotonic 起点
        self._lock = threading.Lock()

    def begin(self) -> None:
        """确认等待开始(进入确认回调前调用)。"""
        # 重复 begin 不重置起点(_active_start 已有值就保持):嵌套确认时内层 begin 若
        # 覆盖起点,外层已经等掉的那段时长就丢算了,预算会被少延长。
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

    Args:
        orig: 回调，原确认回调（如 CLI 的 stdin 确认）
        clock: _UserWaitClock，等待计时器

    Returns:
        包装后的确认回调（语义不变，只在外面加 begin/end 计时）。
    """
    async def wrapped(cr):
        """计时包装：进入前 begin、无论确认/拒绝/异常都在 finally 里 end。

        Args:
            cr: ConfirmRequired，待确认的工具调用

        Returns:
            原确认回调的布尔结果。
        """
        clock.begin()
        try:
            return await orig(cr)
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

    取消级联:父任务被取消(Ctrl+C)时,把取消传播给子任务并等它
    收尾后再抛——aexecute 在父事件循环上直接 await 本协程,级联取消即整棵 agent 树
    一起终止,不再留孤儿子 agent 继续跑、烧 token。

    Args:
        coro: 协程，子 agent 的 run()
        timeout: float，基础执行超时（秒）
        clock: _UserWaitClock，用户等待计时器（等待期间预算持续延长）

    Returns:
        子 agent 的结果；纯执行超时抛 asyncio.TimeoutError，取消沿 await 链级联传播。
    """
    # 用 loop.time() 而非 time.monotonic() 是为了与 asyncio.wait 的时钟同域;两者都是
    # 单调钟,所以下面能与 _UserWaitClock 的 time.monotonic() 直接相减。
    loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(coro)
    base_deadline = loop.time() + timeout
    try:
        while not task.done():
            remaining = (base_deadline + clock.total()) - loop.time()
            if remaining <= 0:
                # 纯执行超时(无用户等待兜底)→ 取消子 agent,抛 TimeoutError。
                # 取消后必须 await 子任务、等它真正收尾再抛:否则子 agent 的清理
                # (落盘、审计收尾、解锁)会与上抛的异常赛跑。吞掉它自己的异常是为了
                # 让上抛的只有"超时"这一件事,不把子任务的报错混进超时语义。
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
        # 父被取消(Ctrl+C):同样先把取消传下去、等子任务收尾,再原样上抛取消——
        # 上抛的是 CancelledError 而非子任务的报错,调用方据此区分"被取消"与"失败"。
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

    Args:
        parent: Agent，父实例（提供 stream_callback）

    Returns:
        子 agent 的流式回调（只透传 tool_start/tool_end）；父无回调时返回 None。
    """
    pcb = getattr(parent, "stream_callback", None)
    if pcb is None:
        return None

    def child_cb(ev: StreamEvent) -> None:
        """只把结构化工具事件透传给父渲染器（前缀由渲染层统一加）。

        Args:
            ev: StreamEvent，子 agent 的流式事件
        """
        if ev.kind in ("tool_start", "tool_end"):
            pcb(ev)          # 前缀由渲染器统一加，此处不再拼 agent_type
    return child_cb


class SpawnSubAgentTool(Tool):
    """派发单个子 agent,返回 SubAgentResult 的序列化结果。

    Attributes:
        name: str，工具名 "spawn_sub_agent"
        description: str，工具描述
        parameters: dict，JSON Schema（agent_type/task）
        needs_parent: bool，True（构造时只注入声明者）
        risk_level: str，"low"
        async_execute: bool，True（父事件循环上直接 await，取消沿链级联）
        timeout: int，子 agent 超时的类默认（config.agents.timeouts 命中时被覆盖）
        _agent_timeouts: dict[str, int]，按 agent 类型的超时覆盖表（config 注入）
        _parent: Agent，父实例（白名单、去重、审计归属都取自它）
    """

    name = "spawn_sub_agent"
    description = ("派发单个 SubAgent 执行子任务，返回结构化结果（status/summary/error_detail/"
                   "digest/needs_attention），digest 为子任务的结构化摘要（提取失败时为空）。"
                   "失败可依据 error_detail 决定重试或上报。")
    parameters = {
        "type": "object",
        "properties": {
            "agent_type": {"type": "string", "description": "目标 SubAgent 类型，如 paper-agent"},
            "task": {"type": "string", "description": "子任务文本（含实体，已拼入上下文）"},
        },
        "required": ["agent_type", "task"],
    }
    #: 需要父 Agent 引用(构造时只注入声明者)
    needs_parent = True
    risk_level = "low"
    #: 异步工具：Agent 在父事件循环上直接 await aexecute——子 agent 与父同循环，
    #: 父被取消（Ctrl+C）时 CancelledError 沿 await 链传播进子 agent，整棵任务树
    #: 级联终止。execute 保留为同步兼容路径
    #: （asyncio.run 包装，供测试/无循环上下文调用）。
    async_execute = True
    #: 子 agent 超时秒数的类默认(config 的 agent_timeouts 命中时被覆盖)。
    #: 保留为类属性:既是无配置时的兜底,也是测试覆盖点(测例可设极小值验证超时路径)。
    timeout = 120

    def __init__(self, agent_timeouts: dict[str, int] | None = None):
        """注入按 agent 类型的超时覆盖表（无表时回退到类属性 timeout）。

        Args:
            agent_timeouts: dict[str, int] | None，agent 类型 → 超时秒数
        """
        # 按 agent 类型的超时覆盖表从 config 注入;无表(如测试直接构造)时
        # 回退到类属性 timeout,既有测例不被破坏。
        self._agent_timeouts = agent_timeouts or {}

    def _resolve_timeout(self, agent_type: str) -> int:
        """解析该 agent 生效超时:配置命中优先,否则类默认。

        Args:
            agent_type: str，目标子 agent 类型

        Returns:
            该类型生效的超时秒数（配置命中优先，否则类默认）。
        """
        return self._agent_timeouts.get(agent_type, self.timeout)

    def execute(self, agent_type: str, task: str) -> ToolResult:
        """同步兼容路径：在调用方线程新建事件循环跑 aexecute。

        Agent 执行器对 async_execute 工具走 aexecute（父循环 await，级联取消）；
        本方法仅供测试/无事件循环上下文直接调用。

        Args:
            agent_type: str，目标子 agent 类型
            task: str，子任务文本

        Returns:
            ToolResult，文本为 SubAgentResult 的 JSON；同步兼容路径（新建事件循环跑 aexecute）。
        """
        return asyncio.run(self.aexecute(agent_type, task))

    def _admit(self, agent_type: str, task: str) -> "ToolResult | str":
        """派发前的五道闸。前两道是「这一路合不合法」的纯判定，后三道是「会不会与别的
        派发冲突、超支」的共享状态检查：

        - 纯判定（不进锁，只读、无共享状态写入）：① 未知 agent 类型 → ② spawn 白名单
        - 共享状态（整体持 _SPAWN_LOCK）：③ 同批同指纹去重 → ④ 审稿预算
          → ⑤ 每轮派发上限

        两条不变式：
        - 所有拒绝都在登记 running **之前**提前 return，注册表不被拒绝路径污染；
        - ④⑤ 只判不记：计数自增与 ③ 的注册收敛在同一个临界区——否则被后续闸拒绝的
          派发会白吃额度。

        顺序与并行由 supervisor 自主决定；被拒/去重的尝试只作为结果返回，不再记账。

        Args:
            agent_type: str，目标子 agent 类型
            task: str，子任务文本

        Returns:
            通过时返回任务指纹字符串——调用方据它清去重条目；拒绝/去重命中时直接返回
            对应状态的 ToolResult。
        """
        parent = self._parent

        # ① 未知类型：先于其余校验，拒绝时附可选清单，让模型能自己改对而不是一路带到构造期
        if agent_type not in parent.agent_registry.list_agents():
            return _deny(f"未知 agent 类型: {agent_type}；可选: "
                         f"{sorted(parent.agent_registry.list_agents())}")

        # ② 白名单：supervisor 硬编码放行，其余按自身 allowed_spawns 校验（单点在 _check_spawn_allowed）
        not_allowed = _check_spawn_allowed(parent, agent_type)
        if not_allowed is not None:
            return _deny(not_allowed)

        # ③~⑤ 触及共享状态（去重注册表 / 预算计数），判定与记账整体持锁。
        # 两者都在 run 容器上（按 trace 隔离，一次用户任务内独立）；容器取用时内部会顺手
        # 清扫过期条目，故此处不再单独清理。
        rs = get_run_state(parent._trace_id)
        fp = _task_fingerprint(task)
        with _SPAWN_LOCK:
            # ③ 去重：同指纹且正在执行中 → 提示等待。只拦同一批工具调用内的机械重复
            #    （模型把同一个调用生成两遍）；不缓存结果，所以跨轮的重复不拦、失败重试会真跑。
            #    键含父实例 id：机械重复来自一次 LLM 生成（一个实例），
            #    按实例分桶足够，且不会让两个兄弟实例的同文本任务互相误拒。
            reg = rs.spawn_registry
            key = (parent._instance_id, fp)
            now = time.monotonic()
            if key in reg:
                # 回传 SubAgentResult 形状（status=denied）而非裸文本：supervisor 统一按
                # status 判读各路 spawn 结果，去重命中要能被同一条判读路径识别。
                result = SubAgentResult(
                    status=SubAgentStatus.DENIED,
                    summary="同任务正在执行中，请等待其结果（已去重，勿重复派发）")
                return ToolResult(text=result.model_dump_json(), summary=result.model_dump())

            # ④ 审稿预算：键 (父实例, mode)——同一 run 内多个同类型父实例各算各的、兄弟不串号；
            #    不同 mode 独立计数。此处只判不记，自增见下方收敛块。
            review_key = (parent._instance_id, agent_type) if agent_type == "review-agent" else None
            if review_key is not None and rs.review_counts.get(review_key, 0) >= _REVIEW_SPAWN_BUDGET:
                return _deny(_REVIEW_BUDGET_DENIED_NOTE.format(budget=_REVIEW_SPAWN_BUDGET))

            # ⑤ 每轮上限：只统计 supervisor 自身的派发，按迭代下标计数——
            #    下一次迭代即重新起算，不会因为上一次迭代派得多而永久锁死。此处同样只判不记。
            turn = getattr(parent, "_current_turn", 0)
            if parent.agent_type == "supervisor" \
                    and rs.turn_spawn_counts.get(turn, 0) >= TURN_SPAWN_BUDGET:
                return _deny(f"本轮派发已达上限 {TURN_SPAWN_BUDGET}，"
                             "请先汇总已有结果向用户交代，需要继续时下一轮再派。")

            # 五道全过：记账收敛到一处——④⑤ 判定阶段只看不写，计数自增与注册
            # running 落在同一临界区，任何一道闸拒绝的派发都不消耗额度。
            if review_key is not None:
                rs.review_counts[review_key] = rs.review_counts.get(review_key, 0) + 1
            if parent.agent_type == "supervisor":
                rs.turn_spawn_counts[turn] = rs.turn_spawn_counts.get(turn, 0) + 1
            reg[key] = now
        return fp

    async def aexecute(self, agent_type: str, task: str) -> ToolResult:
        """派发一个子 agent（父事件循环上 await），返回 SubAgentResult 序列化结果。

        与同步路径同一套门禁与去重；子 agent 与父同循环——取消级联、流式事件、
        审计归属全部天然对齐，不再经工作线程 + 独立事件循环。

        Args:
            agent_type: str，目标子 agent 类型
            task: str，子任务文本

        Returns:
            ToolResult，文本为 SubAgentResult 的 JSON（门禁与去重同同步路径）。
        """
        admitted = self._admit(agent_type, task)
        if isinstance(admitted, ToolResult):
            return admitted
        fp = admitted
        # 派发序列已过闸。此后的收尾只清除本实例本轮记下的「在途」去重条目（登记发生在
        # _admit 里），并按派发结果推进失败计数——容器按作用域取用，与 _admit 的局部变量无关。
        parent = self._parent
        sess = get_session_state(parent.session_id)
        # run 容器：收尾在 finally 里清除本次派发的去重条目（_admit 已登记在它上面）
        rs = get_run_state(parent._trace_id)

        result = None
        try:
            # 构造子 agent(非闸):继承父的安全中间件、会话 ID(同一审计链)与确认回调——
            #    确认回调是关键:note-agent 的写盘工具要求用户确认,不传则默认回调始终拒绝,
            #    spawn 出的 note-agent 永远写不出笔记。不传意图管线/会话 → 子 agent 不做
            #    意图识别(子任务是结构化任务,非用户意图);「问用户」也不是工具,子 agent 需要
            #    用户给信息时把问题写进自己的最终回答,由上级决定是否转达。
            # 流式统一：子 agent 只透传工具行（前缀由渲染器统一加）、不流 content——
            # 与并行场景同一代码路径（多路并发不串字）。
            child = Agent(
                llm=parent.llm, agent_registry=parent.agent_registry,
                skill_registry=getattr(parent, "skill_registry", None),
                agent_type=agent_type, security_middleware=parent.security_middleware,
                session_id=parent.session_id, confirm_callback=parent.confirm_callback,
                stream_callback=_make_child_stream_callback(parent),
                # 继承父 trace_id：去重池（get_run_state 按 trace_id 键控）在
                # 一次用户任务内跨 agent 共享——子 agent 因此不重复下载/抓取父任务
                # 已处理过的资源（父超时重试时会派出新的 paper-agent，不共享池就会重抓）。
                trace_id=getattr(parent, "_trace_id", None),
            )
            # 传解析后的超时:_run_child 用实际生效值(config > 类默认)
            result = await self._run_child(child, agent_type, task)
            # 失败升级：仅 supervisor 的派发计数——连续 N 次非 success
            # 后追加强指令，把「继续自动重试」的决策权交回用户（模型对不可能
            # 成功的任务会自动重派多轮，每轮分钟级）。按会话容器计数：同一会话
            # 内跨 run 累计，成功即清零，换 agent_type 各算各的。
            if parent.agent_type == "supervisor":
                if result.summary.get("status") == SubAgentStatus.SUCCESS:
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
            with _SPAWN_LOCK:
                # 收尾清除本次派发的去重条目（键与 _admit 一致）。只登记在途、不缓存结果：
                # 失败/超时同样立即清除，因此失败重试会真跑并逐次推进失败计数。
                rs.spawn_registry.pop((parent._instance_id, fp), None)
        return result

    async def _run_child(self, child: Agent, agent_type: str, task: str) -> ToolResult:
        """在父事件循环上执行子 agent.run + 提取摘要,映射为 SubAgentResult。

        取消语义:父任务被取消时 CancelledError 沿 await 链传入
        _run_child_with_budget（内部把取消传播给子任务）,再原样上抛——
        _exec_tool 不捕 BaseException,gather 与 run() 的历史自愈随后接力。

        Args:
            child: Agent，已构造的子 agent
            agent_type: str，子 agent 类型
            task: str，子任务文本

        Returns:
            ToolResult；异常映射为 timeout/denied/failed 的 SubAgentResult，取消原样上抛。
        """
        timeout = self._resolve_timeout(agent_type)
        # 用户确认等待不计入执行预算:包装子 agent 的确认回调记录等待时长,
        # _run_child_with_budget 把累积等待加回剩余预算——写盘等用户确认时一直等,
        # 不被超时误杀;纯执行超时仍正常触发。子 agent 的确认回调是构造时继承父的,
        # 此处只外包计时。
        clock = _UserWaitClock()
        child.confirm_callback = _wrap_confirm_callback(child.confirm_callback, clock)

        async def _run_and_extract():
            """先带预算跑子 agent，再对其最终文本提取结构化摘要（摘要不消耗子任务预算）。

            Returns:
                (最终文本, 摘要 dict) 二元组。
            """
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
            result = SubAgentResult(status=SubAgentStatus.SUCCESS, summary=text, digest=digest)
        except asyncio.TimeoutError:
            result = SubAgentResult(status=SubAgentStatus.TIMEOUT, summary="子任务执行超时",
                                    # 插值解析后的超时(配置命中时非类默认),报错可行动
                                    error_detail=f"SubAgent 在 {timeout}s 内未完成")
        except PermissionError as e:
            # 防御性分支:当前架构子 agent 的执行器把策略拒绝/安全拦截降级为普通文本,
            # 不向上抛,几乎不会触发。保留此分支对齐失败处理,不据此推导真实路径。
            result = SubAgentResult(status=SubAgentStatus.DENIED, summary="子任务被策略引擎拒绝",
                                    error_detail=str(e), needs_attention=True)
        except Exception as e:
            result = SubAgentResult(status=SubAgentStatus.FAILED, summary="子任务执行失败",
                                    error_detail=str(e))
        return ToolResult(text=result.model_dump_json(), summary=result.model_dump())
