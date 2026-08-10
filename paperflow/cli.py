"""CLI REPL：交互式 readline,无子命令,/exit 退出。

每轮:读 stdin → 合并挂起的澄清(若有)→ supervisor.run(query, force_dispatch) →
若产生澄清问题且未超轮 → 挂起打印问题;否则打印结果。
跨轮状态由 ConversationState 承载(prev_intent / pending_intent);对话历史经
MessageManager 落盘 SQL 并在每轮 run 回放,超窗口时 compaction 压缩 in-context
窗口(不删 SQL 原始消息)——同一 Supervisor 实例复用其内存/消息管理服务。
"""
import asyncio
import logging
import threading
import uuid
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.llm import LLMClient
from paperflow.core.conversation_state import ConversationState, PendingClarification
from paperflow.core.security import (
    AuditMiddleware, WorkspacePolicyMiddleware,
    SecurityScanMiddleware, PolicyEngineMiddleware,
)
from paperflow.core.structured import StructuredOutput
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.core.memory.services.passage_manager import PassageManager
from paperflow.core.memory.services.archive_manager import ArchiveManager
from paperflow.core.memory.services.tool_manager import ToolManager
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.core.memory.sleeptime import Sleeptime
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.hybrid_router import HybridRouter
from paperflow.rag.embedder import BgeEmbedder, resolve_model_dir
from paperflow.core.intent.route_loader import load_routes

logger = logging.getLogger(__name__)

#: stdin 交互串行锁：并行子 agent 可能同时 ConfirmRequired → 并发读 stdin 提示交错
#: （spec ⚪4，防御性加锁）
_stdin_lock = threading.Lock()

#: 模块级 embedder 单例：bge 模型首次调用才加载（sentence-transformers 导入数秒），
#: 进程内只加载一次。RAG/意图管线/记忆服务共享同一实例——各自 new 一个会让同一
#: 模型权重被反复加载，启动变慢且占内存。
_embedder: "BgeEmbedder | None" = None


def _rag_embedder(config: PaperFlowConfig) -> "BgeEmbedder":
    """懒加载共享 bge embedder（单例）。

    MessageManager/PassageManager 的语义检索与意图管线的稠密路由共用它；模型路径
    本地优先（resolve_model_dir：workspace/models/<name>，否则回退 HF 名）。
    """
    global _embedder
    if _embedder is None:
        _embedder = BgeEmbedder(
            model_name=resolve_model_dir(config.workspace, config.embed_model))
    return _embedder


async def _stdin_confirm(cr) -> bool:
    """yes/no 确认回调(策略引擎触发需确认时调用)。fail-safe:EOF/空 → False。

    确认回调必须是可 await 的(Agent 执行器以 await 方式调用)。
    input() 放到线程执行:确认等待是用户交互,不应冻结共享事件循环——并行派发的
    所有子 agent 共享同一事件循环,同步 input() 会全部卡死。无限等待不取消:写操作
    一直等用户确认,确认时间由子 agent 的预算逻辑排除在超时外,不会因等待而误触发
    超时;无超时 → 无被遗弃的等待线程 → 不会吞掉用户后续输入。
    """
    answer = await asyncio.to_thread(_read_stdin_locked, cr)
    return answer.strip().lower() in {"y", "yes", "是", "确定"}


def _read_stdin_locked(cr) -> str:
    """持 stdin 锁读一行(在线程内调用)。

    并发确认(多个子 agent 同时要确认)经锁串行化、提示不交错;锁只在读行期间持有
    (用户输入到达即释放),不阻塞任何事件循环。EOF(Ctrl-D)→ ""(fail-safe 拒绝)。"""
    with _stdin_lock:
        print(f"[需要确认] {cr.tool_name} 是否继续？(y/N) ", end="", flush=True)
        try:
            return input()
        except EOFError:
            return ""


def _stdin_ask(question: str) -> str:
    """ask_user_question 工具回调：打印问题、读一行返回。"""
    with _stdin_lock:
        print(question)
        try:
            return input("> ").strip()
        except EOFError:
            return ""             # Ctrl-D：返回空串，Supervisor ReAct 自行处理


class _ReplStreamer:
    """把 Agent 流式事件渲染为终端增量输出，并决定最终结果如何打印。

    段模型：root content / child content / tool 三类输出段，段间切换补换行；
    root content 额外缓冲，供 should_print 判断最终答案是否已被逐字展示
    （on_finish 改写如 SAFE_PROMPT 时需要补打最终版）。

    线程安全：on_event 实践上不会被并发调用——root content 来自主 ReAct 的
    chat_stream 线程（串行）；parallel_spawn 子 agent 的 content 被上层包装
    过滤（只留 tool 事件，且都在同一 worker 事件循环上串行）；sequential spawn
    时父 await 子 run，父子流式严格串行。故无锁设计成立。
    """
    def __init__(self, print_fn, root_agent_type: str):
        self._print = print_fn          # 透传 end=/flush=（简单 lambda 会忽略 kwargs）
        self._root = root_agent_type
        self._last_segment = None       # None | "root" | "child" | "tool"
        self._buffer: list[str] = []    # 仅 root content，用于最终答案比对

    def reset(self) -> None:
        """每轮 run 前调用：清空上一轮残留（异常/澄清路径不消费 should_print）。"""
        self._buffer.clear()
        self._last_segment = None

    def on_event(self, ev) -> None:
        if ev.kind == "content":
            seg = "root" if ev.agent_type == self._root else "child"
            # 段切换（root↔child，或 content→content 换段）才补换行；tool→content
            # 不补（工具行已显式终止，游标已在行首）；同段续打不换行
            if self._last_segment in ("root", "child") and self._last_segment != seg:
                self._print("\n", end="")
            self._print(ev.text, end="", flush=True)  # 逐字打字机效果
            if seg == "root":
                self._buffer.append(ev.text)
            self._last_segment = seg
        elif ev.kind == "tool":
            # 工具行：上一段是未自终止的内容（root/child）→ 先补换行结束它；
            # 上一段是 tool（已终止）或 None → 不补，避免空行
            if self._last_segment in ("root", "child"):
                self._print("\n", end="")
            self._print(ev.text, end="", flush=True)
            self._print("\n", end="")  # 工具行显式自终止（不再靠 print 隐式 end）
            if ev.agent_type == self._root:
                self._buffer.clear()   # 工具调用前的中间内容作废，只留最终轮的流式文本
            self._last_segment = "tool"

    def should_print(self, result: str) -> str:
        streamed = "".join(self._buffer)
        if not streamed:
            return result               # 没流式（澄清早退/纯工具轮）→ 维持现状
        if streamed == result:
            return ""                   # 已逐字展示 → print_fn("") 只补换行
        return "\n" + result            # on_finish 改写了（如 SAFE_PROMPT）→ 补打最终版


def _merge_pending(conversation: ConversationState, raw: str) -> tuple[str, bool]:
    """合并跨轮澄清输入,返回 (query, force_dispatch)。

    round < 2 → 合并澄清上下文重跑;round >= 2 → 超轮终止:force_dispatch=True、
    以累积澄清上下文的 original_input 调度——绝不重跑后再次挂起。
    """
    p = conversation.pending_intent
    if p is None:
        return raw, False
    if p.round >= 2:
        conversation.pending_intent = None
        return p.original_input, True
    conversation.pending_intent = None
    return f"{p.original_input}（用户澄清：{raw}）", False


async def _repl(supervisor: Agent, conversation: ConversationState, *,
                input_fn=input, print_fn=print, sleeptime=None) -> None:
    """REPL 主循环。input_fn/print_fn 可注入（测试）。

    sleeptime: 每轮循环顶部触发后台记忆整合（run_once_if_due，未到期/未启用立即
    返回）；None 跳过（测试/无记忆装配）。Sleeptime 失败不打断 REPL——记日志继续。

    澄清挂起：last_intent.clarification 非空且非 force → 存 pending（round 链式累计，
    用旧值 +1，绝不重置为 0）+ 打印问题，等下一轮；否则打印结果。
    """
    print_fn("🌏 paperFlow 学术助手")
    # 流式接线：构造渲染器（print_fn 包装透传 end=/flush= kwargs，简单 lambda
    # 测试也兼容），并把事件回调挂到 supervisor。root_agent_type 用 getattr 兜底
    # （mock supervisor 的 agent_type 可能是自动创建的 MagicMock，测试须显式设置）。
    streamer = _ReplStreamer(
        lambda *a, **k: print_fn(*a, **k),
        root_agent_type=getattr(supervisor, "agent_type", None) or "supervisor",
    )
    supervisor.stream_callback = streamer.on_event
    while True:
        # 每轮循环顶部触发后台记忆整合（取代 Dream）——放在读 stdin 之前，让
        # 用户思考期间累积的对话被整合，整合不阻塞本轮输入。
        if sleeptime is not None:
            try:
                await sleeptime.run_once_if_due()
            except Exception:  # Sleeptime 失败不打断 REPL
                logger.warning("sleeptime tick failed", exc_info=True)
        try:
            raw = input_fn("> ")
        except EOFError:
            break                # Ctrl-D：与 /exit 同效，优雅退出（不吐 traceback）
        if raw.strip() == "/exit":
            break
        p = conversation.pending_intent
        query, force = _merge_pending(conversation, raw)
        streamer.reset()                    # 每轮清残留：异常/澄清路径不消费 should_print
        try:
            result = await supervisor.run(query, force_dispatch=force)
        except MaxTurnsExceeded:
            # 安全阀:LLM 陷入工具调用循环时不杀 REPL——报错并继续,让用户换个更简单
            # 的说法重试(而不是丢挂起状态/整个进程崩溃)
            print_fn("任务超过最大轮数，请简化请求后重试")
            continue
        except Exception as e:
            # 网络失败等不可恢复异常同样不杀 REPL:打印错误,下一轮照常运行。Supervisor
            # 内部已把多数错误转为普通文本反馈,到这里的是真正未预期的异常(如客户端
            # 网络超时)。
            print_fn(f"执行出错：{e}")
            continue
        intent = supervisor.last_intent
        if intent is not None and intent.clarification and not force:
            # 未超轮：挂起澄清，round 链式累计（REPL 重建时用 p.round，不重置为 0）
            prev_round = p.round if p is not None else 0
            conversation.pending_intent = PendingClarification(
                question=intent.clarification, original_input=query,
                round=prev_round + 1)
            print_fn(intent.clarification)
            continue
        print_fn(streamer.should_print(result))


def main() -> None:
    """装配全部依赖并启动 REPL（__main__ 转调）。"""
    config = PaperFlowConfig.from_env()
    llm = LLMClient(config.llm)
    registry = AgentRegistry(config.agents_dir)

    # 会话标识：本次进程启动即一个会话。AgentManager.create_agent 的 agent_id 与
    # Agent.session_id 必须一致——记忆工具（SQL 按 agent_id 键控）与 Sleeptime
    # 都挂在它下面，三者对不上会各自读到空数据。
    session_id = uuid.uuid4().hex[:8]

    # Letta 服务层组装：MemoryDB → managers → 记忆工具播种 → agent 状态。
    # 装配顺序即依赖方向：先 DB，再块/消息/段落管理，再归档（依赖段落管理）、
    # 工具管理（bind 注入服务上下文）、agent 管理（依赖块+消息）。
    memory_dir = Path(config.workspace) / "memory"
    db = MemoryDB(memory_dir / "memory.db")
    block_manager = GitEnabledBlockManager(db, memfs_dir=memory_dir)
    embedder = _rag_embedder(config)
    message_manager = MessageManager(db, embedder=embedder)
    passage_manager = PassageManager(db, embedder=embedder)
    archive_manager = ArchiveManager(db, passage_manager)
    tool_manager = ToolManager(db)
    tool_manager.bind(block_manager, passage_manager, message_manager,
                      agent_id=session_id)
    tool_manager.upsert_base_tools()
    agent_manager = AgentManager(db, block_manager, message_manager)
    # MessageManager 经 agent_manager 读 AgentState.message_ids（in-context 窗口），
    # 让压缩后的摘要/尾部跨轮回放（评审 I-3）——装配顺序上 agent_manager 后置，故在此回填。
    message_manager.agent_manager = agent_manager
    agent_state = agent_manager.create_agent(session_id)

    structured = StructuredOutput(llm)

    # 安全管道：四中间件（经验记忆中间件随 Letta 重构移除——工具调用经验不再注入
    # prompt，改由 Sleeptime 后台整合进核心记忆块）。
    middlewares = [
        AuditMiddleware(),
        WorkspacePolicyMiddleware(workspace=config.workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk=config.max_risk),
    ]

    # 意图管线:真实混合路由器 + LLM 兜底。bge 小模型经 _rag_embedder 共享单例
    # (首次加载需几秒,与记忆服务同模型同实例,不重复加载);各意图阈值已由标定脚本
    # 写回 routes.yaml——这里只读已标定阈值,不做训练或阈值搜索。alpha 是稠密/稀疏
    # 信号的融合权重,与标定脚本保持一致。模型路径本地优先
    # (resolve_model_dir:data/models/<name>,否则回退 HF 名)。
    router = HybridRouter(
        encoder=embedder,
        routes=load_routes(), alpha=0.6)
    pipeline = IntentPipeline(router=router, structured=structured)

    conversation = ConversationState()
    supervisor = Agent(
        llm=llm, agent_registry=registry, agent_type="supervisor",
        memory=agent_state.memory,
        agent_manager=agent_manager, block_manager=block_manager,
        message_manager=message_manager, passage_manager=passage_manager,
        memory_tools=tool_manager.list_tools(),
        compaction=config.compaction,
        structured=structured,
        security_middleware=middlewares,
        intent_enabled=True, intent_pipeline=pipeline, conversation=conversation,
        confirm_callback=_stdin_confirm, ask_user_callback=_stdin_ask,
        session_id=session_id,
    )
    sleeptime = Sleeptime(
        agent_state, block_manager, passage_manager, message_manager,
        structured, enable=config.sleeptime_enable,
        frequency=config.sleeptime_agent_frequency)
    asyncio.run(_repl(supervisor, conversation, sleeptime=sleeptime))
