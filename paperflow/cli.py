"""CLI REPL（spec §6）：交互式 readline，无子命令，/exit 退出。

每轮：读 stdin → 合并 pending（若挂起）→ supervisor.run(query, force_dispatch) →
若 last_intent.clarification 且未超轮 → 挂起打印问题；否则打印结果。
跨轮状态由 Session 承载（prev_intent / pending_intent），复用同一 Supervisor 实例
使 ContextCompressor.summary 跨轮累计。
"""
import asyncio
import logging
import threading
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.llm import LLMClient
from paperflow.core.session import Session, PendingClarification
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
    """yes/no 确认回调（PolicyEngine requires_confirm）。fail-safe：EOF/空 → False。

    async 契约：Agent._exec_tool 以 `await self.confirm_callback(cr)` 调用（agent.py），
    confirm_callback 必须是可 await 的——若此处保持 sync，`await True` 会抛 TypeError，
    generate-note 的写盘工具（requires_confirm=True）在真实 CLI 里永远写不出笔记
    （final review C1，merge blocker）。body 仍是同步的 input()（线程安全用 _stdin_lock），
    仅把函数签名改为 async 以满足 await 契约。
    """
    with _stdin_lock:
        try:
            answer = input(f"[需要确认] {cr.tool_name} 是否继续？(y/N) ").strip().lower()
        except EOFError:
            return False          # Ctrl-D：fail-safe 拒绝（spec §6.3 承诺的 EOF 语义）
        return answer in {"y", "yes", "是", "确定"}


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
            if self._last_segment not in (None, seg):
                self._print("\n")                    # 段切换补换行
            self._print(ev.text, end="", flush=True) # 逐字打字机效果
            if seg == "root":
                self._buffer.append(ev.text)
            self._last_segment = seg
        elif ev.kind == "tool":
            self._print("\n")
            self._print(ev.text, flush=True)
            if ev.agent_type == self._root:
                self._buffer.clear()    # 工具调用前的中间内容作废，只留最终轮的流式文本
            self._last_segment = "tool"

    def should_print(self, result: str) -> str:
        streamed = "".join(self._buffer)
        if not streamed:
            return result               # 没流式（澄清早退/纯工具轮）→ 维持现状
        if streamed == result:
            return ""                   # 已逐字展示 → print_fn("") 只补换行
        return "\n" + result            # on_finish 改写了（如 SAFE_PROMPT）→ 补打最终版


def _merge_pending(session: Session, raw: str) -> tuple[str, bool]:
    """合并跨轮澄清输入，返回 (query, force_dispatch)。

    round < 2 → 合并澄清上下文重跑；round >= 2 → 超轮终止：force_dispatch=True、
    以累积澄清上下文的 original_input 调度（D9）——绝不重跑后再次挂起。
    """
    p = session.pending_intent
    if p is None:
        return raw, False
    if p.round >= 2:
        session.pending_intent = None
        return p.original_input, True
    session.pending_intent = None
    return f"{p.original_input}（用户澄清：{raw}）", False


async def _repl(supervisor: Agent, session: Session, *,
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
        p = session.pending_intent
        query, force = _merge_pending(session, raw)
        streamer.reset()                    # 每轮清残留：异常/澄清路径不消费 should_print
        try:
            result = await supervisor.run(query, force_dispatch=force)
        except MaxTurnsExceeded:
            # 安全阀（D10 降级哲学）：LLM 陷入 tool-call 循环时不杀 REPL——报错并继续，
            # 让用户能换一种更简单的说法重试（而不是丢 pending 状态/整进程崩溃）
            print_fn("任务超过最大轮数，请简化请求后重试")
            continue
        except Exception as e:
            # 网络失败等不可恢复异常同样不杀 REPL（D10）：打印错误，下一轮照常运行。
            # 这是降级声明的兜底——Supervisor.run 内部已把多数错误 degrade-to-text，
            # 到这里的是真正未预期的异常（如 LLM 客户端网络超时）。
            print_fn(f"执行出错：{e}")
            continue
        intent = supervisor.last_intent
        if intent is not None and intent.clarification and not force:
            # 未超轮：挂起澄清，round 链式累计（REPL 重建时用 p.round，不重置为 0）
            prev_round = p.round if p is not None else 0
            session.pending_intent = PendingClarification(
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

    # 意图管线：真实 HybridRouter + LLM 兜底（Layer 1 装配，Stage 0/1 已接线）。
    # 【真实 bge（D12/D13）】BgeEmbedder 加载 bge-small-zh-v1.5（~30MB/几秒，RAG 栈同模型
    # 的独立实例）；阈值由 scripts/verify_intent.py 标定后写回 routes.yaml——这里只
    # load_routes 读已标定 per-route 阈值，零 fit、零阈值搜索（启动只读配置不算配置）。
    # alpha=0.6：gate 驱动重标定（0.3 是 md5 伪向量时代的默认，真实 bge 下稠密信号应
    # 主导），由 verify_intent 在 eval 集上选定——与 verify_intent 的 ALPHA 保持一致。
    # 模型路径本地优先（resolve_model_dir：data/models/<name>，回退 HF 名）
    router = HybridRouter(
        encoder=BgeEmbedder(model_name=resolve_model_dir(config.workspace, config.embed_model)),
        routes=load_routes(), alpha=0.6)
    pipeline = IntentPipeline(router=router, structured=structured)

    session = Session()
    supervisor = Agent(
        llm=llm, agent_registry=registry, agent_type="supervisor",
        security_middleware=middlewares,
        memory_index=MemoryIndex(memory_dir),
        compressor=ContextCompressor(config.context, llm, structured=structured),
        intent_enabled=True, intent_pipeline=pipeline, session=session,
        confirm_callback=_stdin_confirm, ask_user_callback=_stdin_ask,
    )
    dream = Dream(store=store, git=git, llm=llm, structured=structured)
    asyncio.run(_repl(supervisor, session, dream=dream))
