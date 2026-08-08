"""CLI REPL：交互式 readline,无子命令,/exit 退出。

每轮:读 stdin → 合并挂起的澄清(若有)→ supervisor.run(query, force_dispatch) →
若产生澄清问题且未超轮 → 挂起打印问题;否则打印结果。
跨轮状态由 ConversationState 承载(prev_intent / pending_intent),复用同一 Supervisor 实例使
上下文压缩器的历史跨轮累积。
"""
import asyncio
import logging
import threading
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
from paperflow.core.memory import (
    MemoryStore, ExperienceMemoryMiddleware, MemoryIndex,
    ContextCompressor, GitStore, Dream,
)
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.hybrid_router import HybridRouter
from paperflow.rag.embedder import BgeEmbedder, resolve_model_dir
from paperflow.core.intent.route_loader import load_routes

logger = logging.getLogger(__name__)

#: stdin 交互串行锁：并行子 agent 可能同时 ConfirmRequired → 并发读 stdin 提示交错
#: （spec ⚪4，防御性加锁）
_stdin_lock = threading.Lock()


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
    """AskUserTool 回调：打印问题、读一行返回。"""
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
                input_fn=input, print_fn=print, dream=None) -> None:
    """REPL 主循环。input_fn/print_fn 可注入（测试）。

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
        if dream is not None:
            try:
                await dream.run_once_if_due()
            except Exception:  # Dream 失败不打断 REPL
                logger.warning("dream tick failed", exc_info=True)


def main() -> None:
    """装配全部依赖并启动 REPL（__main__ 转调）。"""
    config = PaperFlowConfig.from_env()
    llm = LLMClient(config.llm)
    registry = AgentRegistry(config.agents_dir)

    memory_dir = Path(config.workspace) / "memory"
    store = MemoryStore(memory_dir)
    git = GitStore(memory_dir)
    structured = StructuredOutput(llm, store=store)

    middlewares = [
        AuditMiddleware(),
        WorkspacePolicyMiddleware(workspace=config.workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk=config.max_risk),
        ExperienceMemoryMiddleware(store),
    ]

    # 意图管线:真实混合路由器 + LLM 兜底。BgeEmbedder 加载 bge 小模型(首次加载需
    # 几秒,与 RAG 栈同模型但独立实例);各意图阈值已由标定脚本写回 routes.yaml——这里
    # 只读已标定阈值,不做训练或阈值搜索。alpha 是稠密/稀疏信号的融合权重,与标定脚本
    # 保持一致。模型路径本地优先(resolve_model_dir:data/models/<name>,否则回退 HF 名)。
    router = HybridRouter(
        encoder=BgeEmbedder(model_name=resolve_model_dir(config.workspace, config.embed_model)),
        routes=load_routes(), alpha=0.6)
    pipeline = IntentPipeline(router=router, structured=structured)

    conversation = ConversationState()
    supervisor = Agent(
        llm=llm, agent_registry=registry, agent_type="supervisor",
        security_middleware=middlewares,
        memory_index=MemoryIndex(memory_dir),
        compressor=ContextCompressor(config.context, llm, structured=structured),
        intent_enabled=True, intent_pipeline=pipeline, conversation=conversation,
        confirm_callback=_stdin_confirm, ask_user_callback=_stdin_ask,
    )
    dream = Dream(store=store, git=git, llm=llm, structured=structured)
    asyncio.run(_repl(supervisor, conversation, dream=dream))
