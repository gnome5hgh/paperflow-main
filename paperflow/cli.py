"""CLI REPL：交互式 readline,无子命令,/exit 退出。

每轮:读 stdin → 合并挂起的澄清(若有)→ supervisor.run(query, force_dispatch) →
若产生澄清问题且未超轮 → 挂起打印问题;否则打印结果。
跨轮状态由 ConversationState 承载(prev_intent / pending_intent);对话历史经
MessageManager 落盘 SQL 并在每轮 run 回放,超窗口时 compaction 压缩 in-context
窗口(不删 SQL 原始消息)——同一 Supervisor 实例复用其内存/消息管理服务。

终端交互经 paperflow/terminal 子包隔离:InputIO(输入适配,TTY=prompt_toolkit,
非 TTY=FallbackIO)与 StreamRenderer(输出渲染,TTY=rich Live,非 TTY=PlainBlock)。
"""
import asyncio
import logging
import signal
import sys
import uuid
from pathlib import Path

from rich.console import Console

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent, MaxTurnsExceeded
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.llm import LLMClient
from paperflow.core.intent.conversation_state import ConversationState, PendingClarification
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
from paperflow.core.memory.services.title_extractor import TitleExtractor
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.core.memory.sleeptime import Sleeptime
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.routing.router import HybridRouter
from paperflow.rag.encoders.embedder import BgeEmbedder, resolve_model_dir
from paperflow.rag.parsers.grobid_client import GrobidClient
from paperflow.core.intent.routing.route_loader import load_routes
from paperflow.terminal.io import InputIO, make_input_io
from paperflow.terminal.render import StreamRenderer, make_renderer

logger = logging.getLogger(__name__)

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


def _make_print_fn(console):
    """生产 print_fn。console 非 None（TTY）经 rich 输出（style 生效，工具行 dim）；
    console 为 None（非 TTY）用内置 print（吸收 style，不产生 ANSI）。"""
    if console is None:
        def _plain(*args, style=None, **kwargs):
            print(*args, **kwargs)
        return _plain

    def _rich(*args, style=None, end="\n", flush=False):
        console.print(*args, style=style, end=end, overflow="ignore")
    return _rich


def _make_confirm_callback(io: InputIO):
    """构造 async 确认回调（Agent 执行器以 await 方式调用）。

    确认等待是用户交互，input()/prompt() 放到线程执行——不应冻结共享事件循环；
    to_thread 包一层，与改造前 _stdin_confirm 的线程模型一致。fail-safe：EOF 与
    Ctrl+C（TTY 下确认框的 c-c 键绑定抛 KeyboardInterrupt）都返回 False——拒绝；
    TTY 下 prompt_toolkit 不捕 EOFError，这里再兜一层（两实现可替换）。
    """
    async def _confirm(cr) -> bool:
        try:
            return await asyncio.to_thread(
                io.confirm, f"[需要确认] {cr.tool_name} 是否继续？(y/N) ")
        except (EOFError, KeyboardInterrupt):
            return False
    return _confirm


def _make_ask_callback(io: InputIO):
    """构造 ask_user 回调：读开放问题答案，Ctrl-D/EOF/Ctrl+C → 空串。

    PromptToolkitIO（TTY）的 ask 不捕 EOFError，这里兜底返回空串（与非 TTY
    FallbackIO 行为一致，两实现可替换）；Supervisor ReAct 收到空串自行处理。
    """
    def _ask(question: str) -> str:
        try:
            return io.ask(question)
        except (EOFError, KeyboardInterrupt):
            return ""
    return _ask


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
                io: InputIO, renderer: StreamRenderer, sleeptime=None) -> None:
    """REPL 主循环。io/renderer 可注入（测试）。

    Ctrl+C 三态：输入框空（io.read 抛 KeyboardInterrupt）→ 退出；输入框有内容 →
    输入框内清空（PromptToolkitIO 键绑定）；agent 运行中 → 临时注册 SIGINT handler
    取消当前 run 任务 → 捕获 CancelledError → 打印「已中断」回到输入框（不杀 REPL）。
    SIGINT handler 只在 run 期间存在：prompt_toolkit 提示期间无冲突（Ctrl+C 由其
    键绑定处理，终端处 raw 模式不产生 SIGINT）。

    澄清挂起：last_intent.clarification 非空且非 force → 存 pending（round 链式
    累计，用旧值 +1，绝不重置为 0）+ 打印问题，等下一轮；否则打印结果。
    """
    renderer.print("🌏 paperFlow 学术助手")
    supervisor.stream_callback = renderer.on_event
    loop = asyncio.get_running_loop()
    can_sigint = (hasattr(loop, "add_signal_handler")
                  and hasattr(loop, "remove_signal_handler"))
    run_task = None
    read_failures = 0

    def _cancel_run():
        if run_task is not None and not run_task.done():
            run_task.cancel()

    while True:
        # 每轮循环顶部触发后台记忆整合——放在读 stdin 之前，让用户思考期间累积的
        # 对话被整合，整合不阻塞本轮输入。
        if sleeptime is not None:
            try:
                await sleeptime.run_once_if_due()
            except Exception:  # Sleeptime 失败不打断 REPL
                logger.warning("sleeptime tick failed", exc_info=True)
        try:
            # io.read 必须经 to_thread 在 worker 线程执行：PromptToolkitIO.read 内部
            # session.prompt() 会自建事件循环（asyncio.run），而 _repl 跑在主事件循环
            # 线程——直接同步调用抛 "asyncio.run() cannot be called from a running
            # event loop"（实测复现）。confirm/ask 回调已是 to_thread，read 对齐之。
            raw = await asyncio.to_thread(io.read, "> ")
        except (EOFError, KeyboardInterrupt):
            break                # Ctrl-D / 空框 Ctrl+C：与 /exit 同效，优雅退出
        except Exception as e:
            # 输入适配器故障不杀 REPL（spec §5）：打印后继续；但连续失败说明故障是
            # 持久的，无限刷错误比退出更糟——3 次后放弃。
            read_failures += 1
            renderer.print(f"输入出错：{e}")
            if read_failures >= 3:
                renderer.print("连续输入失败，退出")
                break
            continue
        read_failures = 0
        if raw.strip() == "/exit":
            break
        p = conversation.pending_intent
        query, force = _merge_pending(conversation, raw)
        renderer.reset()                    # 每轮清残留：异常/澄清路径不消费 should_print
        # 先注册 SIGINT handler 再 create_task：注册与建任务之间的同步间隙若落一个
        # SIGINT，默认 handler 会在主线程抛 KeyboardInterrupt 崩 REPL。handler 已就位
        # 则 _cancel_run 吞掉它（run_task 尚未赋值 → no-op，不崩）。
        run_task = None
        if can_sigint:
            try:
                loop.add_signal_handler(signal.SIGINT, _cancel_run)
            except (NotImplementedError, RuntimeError):
                # 信号注册失败（如非主线程/平台不支持）→ 降级为默认 Ctrl+C，不崩 REPL
                can_sigint = False
        run_task = asyncio.create_task(supervisor.run(query, force_dispatch=force))
        try:
            result = await run_task
        except asyncio.CancelledError:
            # Ctrl+C 优雅中断：渲染器过滤孤儿事件（to_thread 无法真正取消）、打印
            # 提示、回到输入框。安全阀语义与 MaxTurnsExceeded 一致——不杀 REPL。
            renderer.interrupt()
            renderer.print("已中断")
            continue
        except MaxTurnsExceeded:
            renderer.print("任务超过最大轮数，请简化请求后重试")
            continue
        except Exception as e:
            renderer.print(f"执行出错：{e}")
            continue
        finally:
            if can_sigint:
                try:
                    loop.remove_signal_handler(signal.SIGINT)
                except (NotImplementedError, RuntimeError):
                    pass
        intent = supervisor.last_intent
        if intent is not None and intent.clarification and not force:
            # 未超轮：挂起澄清，round 链式累计（REPL 重建时用 p.round，不重置为 0）
            prev_round = p.round if p is not None else 0
            conversation.pending_intent = PendingClarification(
                question=intent.clarification, original_input=query,
                round=prev_round + 1)
            renderer.print(intent.clarification)
            continue
        renderer.finalize()
        renderer.print(renderer.should_print(result))


def main() -> None:
    """装配全部依赖并启动 REPL（__main__ 转调）。"""
    config = PaperFlowConfig.from_env()
    is_tty = sys.stdin.isatty()
    io = make_input_io(config)
    console = Console() if is_tty else None
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
    block_manager.ensure_default_blocks()   # 首启播种默认 persona/human 核心记忆块
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

    # extract_title 工具的标题提取器注入 ctx（LLM 层走 StructuredOutput 真实接线）。
    # GROBID 层用 config.grobid_endpoint 装配：extract_title 走本地 REST header
    # 接口，不可达或解析失败时返回 None，自动落到 LLM 层兜底。
    tool_manager._ctx.title_extractor = TitleExtractor(
        grobid=GrobidClient(config.grobid_endpoint),
        llm=structured)

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
        confirm_callback=_make_confirm_callback(io),
        ask_user_callback=message_manager.make_ask_recorder(_make_ask_callback(io),
                                                            session_id),
        session_id=session_id,
    )
    sleeptime = Sleeptime(
        agent_state, block_manager, passage_manager, message_manager,
        structured, enable=config.sleeptime_enable,
        frequency=config.sleeptime_agent_frequency)

    # 终端装配：TTY → prompt_toolkit 输入 + rich Live 渲染；非 TTY（管道/CI/测试）→
    # FallbackIO + PlainBlock 降级（行为与改造前一致）。renderer 的 root_agent_type
    # 用于 content 段归属判别（getattr 兜底 mock supervisor）。
    renderer = make_renderer(
        _make_print_fn(console),
        getattr(supervisor, "agent_type", None) or "supervisor",
        is_tty=is_tty, console=console,
    )
    asyncio.run(_repl(supervisor, conversation,
                      io=io, renderer=renderer, sleeptime=sleeptime))
