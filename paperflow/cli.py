# paperflow/cli.py
"""CLI REPL：交互式终端,无子命令,/exit 退出。

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

from paperflow.bootstrap import ensure_services
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
from paperflow.tools.memory import set_memory_context, MemoryToolsContext
from paperflow.core.memory.services.title_extractor import TitleExtractor
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.core.memory.sleeptime import Sleeptime
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.routing.router import HybridRouter
from paperflow.rag.encoders.embedder import BgeEmbedder, resolve_model_dir
from paperflow.rag.parsers.grobid_client import GrobidClient
from paperflow.core.intent.routing.route_loader import load_routes
from paperflow.terminal.diff import compute_diff, truncate_diff
from paperflow.terminal.io import InputIO, make_input_io
from paperflow.terminal.render import StreamRenderer, make_renderer

logger = logging.getLogger(__name__)

#: 模块级 embedder 单例：bge 模型首次调用才加载（sentence-transformers 导入数秒），
#: 进程内只加载一次。RAG/意图管线/记忆服务共享同一实例——各自 new 一个会让同一
#: 模型权重被反复加载，启动变慢且占内存。
_embedder: "BgeEmbedder | None" = None


def _rag_embedder(config: PaperFlowConfig) -> "BgeEmbedder":
    """
    懒加载共享的 BGE 嵌入模型单例。

    用途：
        - MessageManager / PassageManager 的语义检索
        - 意图管线的稠密路由（HybridRouter）
    所有组件共享同一实例，避免重复加载模型权重（首次加载需数秒，且占用内存）。

    Args:
        config: 全局配置，包含 workspace 和 embed_model 名称。

    Returns:
        BgeEmbedder: 共享的嵌入模型实例。

    Notes:
        - 模型路径优先本地：resolve_model_dir 在 workspace/models/<name> 查找，
          若不存在则回退 HuggingFace 缓存。
        - 该函数在进程生命周期内只加载一次。
    """
    global _embedder
    if _embedder is None:
        _embedder = BgeEmbedder(
            model_name=resolve_model_dir(config.workspace, config.embed_model))
    return _embedder


def _make_print_fn(console):
    """
    根据终端类型构造打印函数。

    Args:
        console: rich.Console 实例，若为 None（非 TTY）则使用内置 print。

    Returns:
        打印函数，接受 *args, style=None, end="\n", flush=False 等参数。
            若 console 非 None，使用 console.print 并支持 style（如 dim 样式）；
            若 console 为 None，使用内置 print 并忽略 style（不产生 ANSI 转义）。
    """
    if console is None:
        def _plain(*args, style=None, **kwargs):
            print(*args, **kwargs)
        return _plain

    def _rich(*args, style=None, end="\n", flush=False):
        # overflow="fold"：长行自动换行而非截断
        console.print(*args, style=style, end=end, overflow="fold")
    return _rich


def _confirm_diff_preview(tool_name: str, params: dict) -> str | None:
    """
    为写/编辑工具生成确认前的 diff 预览文本。

    Args:
        tool_name: 工具名称（"write_file" 或 "edit_file"）。
        params: 工具参数字典，需包含 "path"，write_file 还需 "content"，
                edit_file 还需 "old_text" 和 "new_text"。

    Returns:
        str | None: 若预览可用则返回截断后的 unified diff 字符串；
                    若工具不是写/编辑、参数缺失、文件读取失败或编辑替换条件不满足，
                    则返回 None（表示走纯确认，无预览）。

    关键边界条件（edit_file）：
        - edit_file 工具仅在 old_text 在文件中恰好出现一次时才执行替换。
          若 count != 1，则实际不会写入，此时预览与当前内容无异，反而干扰用户，
          因此直接返回 None，只走纯确认。
        - 若文件不存在，old 为空字符串，count=0，亦返回 None。
    """
    if tool_name not in {"write_file", "edit_file"}:
        return None
    path = params.get("path") if isinstance(params, dict) else None
    if not path:
        return None
    p = Path(path)
    try:
        old = p.read_text(encoding="utf-8") if p.exists() else ""
    except (OSError, UnicodeDecodeError):
        return None
    if tool_name == "write_file":
        new = params.get("content", "")
    else:  # edit_file
        old_text, new_text = params.get("old_text"), params.get("new_text")
        if old_text is None or new_text is None:
            return None
        # 仅在替换确实会应用时预览：要求 old_text 在文件中恰好出现一次
        if old.count(old_text) != 1:
            return None
        new = old.replace(old_text, new_text)
    return truncate_diff(compute_diff(old, new, fromfile=str(p), tofile=str(p)))


def _make_confirm_callback(io: InputIO, renderer: StreamRenderer):
    """
    构造异步确认回调函数，供 Agent 执行器在工具执行前调用。

    Args:
        io: 输入适配器（用于读取用户确认）。
        renderer: 渲染器（用于显示 diff 预览和暂停 live）。

    Returns:
        async callable: 接收一个 ConfirmRequest 对象，返回 bool（True 表示确认继续）。

    行为：
        1. 若工具是写/编辑，先计算 diff 预览并通过 renderer.print_diff 显示。
        2. 调用 renderer.suspend() 停止实时渲染（避免与 prompt_toolkit 提示框冲突）。
        3. 通过 asyncio.to_thread 在单独线程中执行 io.confirm（避免阻塞事件循环）。
        4. 捕获 EOFError/KeyboardInterrupt 返回 False（保守拒绝）。
    """
    async def _confirm(cr) -> bool:
        preview = _confirm_diff_preview(cr.tool_name, getattr(cr, "params", None))
        if preview:
            renderer.print_diff(preview)
        # 确认框前无条件停 live（spinner/残留内容块）：rich Live 与 prompt_toolkit
        # 提示框并发会互相干扰（方向键不响应）。print_diff 已停一次，这里兜底。
        renderer.suspend()
        try:
            return await asyncio.to_thread(
                io.confirm, f"[Confirm] {cr.tool_name}?")
        except (EOFError, KeyboardInterrupt):
            return False
    return _confirm


def _make_ask_callback(io: InputIO, renderer: StreamRenderer):
    """
    构造 ask_user 回调：读取开放问题的答案。

    Args:
        io: 输入适配器。
        renderer: 渲染器（用于暂停 live）。

    Returns:
        callable: 接受 question 字符串，返回答案字符串。
                  遇到 EOF/Ctrl+C 返回空串（fail-safe）。

    行为：
        - 先 suspend 停止 live 渲染，避免与输入框冲突。
        - 调用 io.ask，捕获异常返回空串。
    """
    def _ask(question: str) -> str:
        renderer.suspend()
        try:
            return io.ask(question)
        except (EOFError, KeyboardInterrupt):
            return ""
    return _ask


def _merge_pending(conversation: ConversationState, raw: str) -> tuple[str, bool]:
    """
    合并跨轮澄清输入，返回 (query, force_dispatch)。

    当上一轮产生了澄清问题且未超轮（round < 2），本轮输入视为对澄清的回答，
    将其与原始查询拼接作为新查询，并设置 force_dispatch=False 以便重新运行 intent 管线。
    若澄清轮数已达上限（round >= 2），则强制调度（force_dispatch=True），
    使用原始查询（不含用户澄清）直接进入 ReAct 循环，避免无限澄清循环。

    Args:
        conversation: 会话状态（包含 pending_intent）。
        raw: 当前轮的用户输入。

    Returns:
        (query: str, force_dispatch: bool)
            - query: 实际用于 supervisor.run 的查询文本。
            - force_dispatch: 若为 True，则跳过意图识别，直接执行 ReAct 循环。
    """
    p = conversation.pending_intent
    if p is None:
        return raw, False
    if p.round >= 2:
        # 超轮：强制调度，清除 pending 状态
        conversation.pending_intent = None
        return p.original_input, True
    # 未超轮：合并澄清内容，清除 pending
    conversation.pending_intent = None
    return f"{p.original_input}（用户澄清：{raw}）", False


def _shorten_path(p: str) -> str:
    """将路径中的 home 目录缩写为 '~'，用于 banner 显示。"""
    home = str(Path.home())
    return "~" + p[len(home):] if str(p).startswith(home) else str(p)


def _render_banner(model: str, workspace: str) -> str:
    """
    生成启动横幅，使用 box-drawing 字符绘制方框。

    Args:
        model: 模型名称（如 "gpt-4"）。
        workspace: 工作区路径（已缩写）。

    Returns:
        str: 多行横幅字符串，包含标题、模型和工作区信息。
    """
    lines = [
        ">_ paperFlow Academic Assistant",
        "",
        f"model:     {model}",
        f"workspace: {workspace}",
    ]
    inner = max(len(l) for l in lines)
    top = "╭" + "─" * (inner + 2) + "╮"
    body = "\n".join(f"│ {l:<{inner}} │" for l in lines)
    bottom = "╰" + "─" * (inner + 2) + "╯"
    return f"{top}\n{body}\n{bottom}"


async def _repl(supervisor: Agent, conversation: ConversationState, *,
                io: InputIO, renderer: StreamRenderer, sleeptime=None,
                config: PaperFlowConfig | None = None) -> None:
    """
    REPL 主循环。

    每轮：
        1. 触发后台记忆整合（Sleeptime）。
        2. 读取用户输入（通过 io.read，工作线程）。
        3. 若有挂起的澄清，合并查询（_merge_pending）。
        4. 重置渲染器（renderer.reset），注册 SIGINT 处理器以取消运行中的任务。
        5. 异步执行 supervisor.run(query, force_dispatch)。
        6. 根据结果：
            - 若任务被取消（Ctrl+C）：打印 "Cancelled"，继续循环。
            - 若超轮：提示并继续。
            - 若产生澄清且未强制：挂起澄清（pending_intent），打印问题，继续下一轮。
            - 否则：结束渲染（finalize），根据 should_print 决定是否打印最终答案。

    Ctrl+C 三态处理：
        - 输入框为空时：io.read 抛出 KeyboardInterrupt，退出 REPL。
        - 输入框有内容时：PromptToolkitIO 键绑定清空输入框，不退出。
        - Agent 运行时：SIGINT 处理器取消当前 run_task，捕获 CancelledError 后继续。

    Args:
        supervisor: 主 Agent 实例。
        conversation: 跨轮状态。
        io: 输入适配器。
        renderer: 渲染器。
        sleeptime: 后台记忆整合调度器（可选）。
        config: 配置（仅用于横幅，若为 None 则从环境加载）。
    """
    cfg = config or PaperFlowConfig.from_env()
    renderer.print(_render_banner(cfg.llm.model, _shorten_path(cfg.workspace)))
    renderer.print("\n  Tip: Type a research task to begin, or /exit to quit")
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
            # 线程——直接同步调用会抛 "asyncio.run() cannot be called from a running
            # event loop"。confirm/ask 回调已是 to_thread，read 对齐之。
            raw = await asyncio.to_thread(io.read, "> ")
        except (EOFError, KeyboardInterrupt):
            break                # Ctrl-D / 空框 Ctrl+C：与 /exit 同效，优雅退出
        except Exception as e:
            # 输入适配器故障不杀 REPL：打印后继续；但连续失败说明故障是持久的，
            # 无限刷错误比退出更糟——3 次后放弃。
            read_failures += 1
            renderer.print(f"Input error: {e}")
            if read_failures >= 3:
                renderer.print("Input failing repeatedly. Exiting.")
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
            renderer.print("Cancelled")
            continue
        except MaxTurnsExceeded:
            renderer.print("Task exceeded max turns. Please rephrase and retry.")
            continue
        except Exception as e:
            renderer.print(f"Error: {e}")
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
    """
    装配全部依赖并启动 REPL。

    装配顺序（依赖关系）：
        1. 终端 IO 和渲染器（输入/输出适配）。
        2. 会话 ID（用于记忆服务键控）。
        3. 记忆服务层：DB → BlockManager → MessageManager → PassageManager → ArchiveManager → AgentManager。
        4. 嵌入模型（单例）注入 MessageManager/PassageManager。
        5. AgentManager 回填到 MessageManager（用于读取 AgentState）。
        6. 创建 AgentState 和结构化输出。
        7. 设置记忆工具上下文（包括标题提取器）。
        8. 构造安全中间件、意图管线。
        9. 构造 Supervisor Agent 和 Sleeptime。
        10. 运行 REPL 主循环。

    关键依赖顺序：
        - AgentManager 依赖 BlockManager 和 MessageManager；MessageManager 需要 AgentManager 来获取 in-context 窗口，
          因此创建顺序为：先建 AgentManager，再回填 MessageManager.agent_manager。
        - 记忆工具上下文需要 TitleExtractor，它依赖 GrobidClient 和 StructuredOutput。
    """
    config = PaperFlowConfig.from_env()
    is_tty = sys.stdin.isatty()
    io = make_input_io(config)
    console = Console() if is_tty else None
    # 启动预检：依赖服务（Milvus/GROBID）未起时自动 docker compose 拉起（仅 TTY，
    # 管道/CI 跳过）。软依赖：任何失败只产出警告不阻塞——服务缺席时 RAG/PDF 降级。
    service_warnings = ensure_services(
        config, is_tty=is_tty,
        notify=(lambda msg: console.print(msg, style="dim")) if console else None)
    for w in service_warnings:
        (console.print(w, style="yellow") if console else print(w))
    llm = LLMClient(config.llm)
    registry = AgentRegistry(config.agents_dir)

    # 终端装配：TTY → prompt_toolkit 输入 + rich Live 渲染；非 TTY（管道/CI/测试）→
    # FallbackIO + PlainBlock 降级。renderer 须在 supervisor 前构造——confirm_callback
    # （写/编辑确认 diff 预览）是 supervisor 构造参数。
    # root_agent_type 恒为 "supervisor"（根 agent 的 agent_type 见 supervisor 构造处）。
    renderer = make_renderer(
        _make_print_fn(console),
        "supervisor",
        is_tty=is_tty, console=console,
    )

    # 会话标识：本次进程启动即一个会话。AgentManager.create_agent 的 agent_id 与
    # Agent.session_id 必须一致——记忆工具（SQL 按 agent_id 键控）与 Sleeptime
    # 都挂在它下面，三者对不上会各自读到空数据。
    session_id = uuid.uuid4().hex[:8]

    # 记忆服务层组装：MemoryDB → managers → set_memory_context 绑定记忆工具
    # 运行时上下文 → agent 状态。装配顺序即依赖方向：先 DB，再块/消息/段落管理，
    # 再归档（依赖段落管理）、agent 管理（依赖块+消息）。
    memory_dir = Path(config.workspace) / "memory"
    db = MemoryDB(memory_dir / "memory.db")
    block_manager = GitEnabledBlockManager(db, memfs_dir=memory_dir)
    block_manager.ensure_default_blocks()   # 首启播种默认 persona/human 核心记忆块
    embedder = _rag_embedder(config)
    message_manager = MessageManager(db, embedder=embedder)
    passage_manager = PassageManager(db, embedder=embedder)
    archive_manager = ArchiveManager(db, passage_manager)
    agent_manager = AgentManager(db, block_manager, message_manager)
    # MessageManager 经 agent_manager 读 AgentState.message_ids（in-context 窗口），
    # 压缩后的摘要/尾部要跨轮回放——装配顺序上 agent_manager 后置，故在此回填。
    message_manager.agent_manager = agent_manager
    agent_state = agent_manager.create_agent(session_id)

    structured = StructuredOutput(llm)

    # extract_title 工具的标题提取器注入记忆工具运行时上下文（LLM 层走
    # StructuredOutput 真实接线）。GROBID 层用 config.grobid_endpoint 装配：
    # extract_title 走本地 REST header 接口，不可达或解析失败时返回 None，
    # 自动落到 LLM 层兜底。
    set_memory_context(MemoryToolsContext(
        agent_id=session_id,
        block_manager=block_manager,
        passage_manager=passage_manager,
        message_manager=message_manager,
        title_extractor=TitleExtractor(grobid=GrobidClient(config.grobid_endpoint),
                                       llm=structured),
    ))

    # 安全管道：四中间件（经验记忆中间件已移除——工具调用经验不再注入 prompt，
    # 改由 Sleeptime 后台整合进核心记忆块）。
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
        routes=load_routes(), alpha=0.5)
    pipeline = IntentPipeline(router=router, structured=structured)

    conversation = ConversationState()

    supervisor = Agent(
        llm=llm, agent_registry=registry, agent_type="supervisor",
        memory=agent_state.memory,
        agent_manager=agent_manager, block_manager=block_manager,
        message_manager=message_manager, passage_manager=passage_manager,
        compaction=config.compaction,
        structured=structured,
        security_middleware=middlewares,
        intent_enabled=True, intent_pipeline=pipeline, conversation=conversation,
        confirm_callback=_make_confirm_callback(io, renderer),
        ask_user_callback=message_manager.make_ask_recorder(_make_ask_callback(io, renderer),
                                                            session_id),
        session_id=session_id,
    )
    sleeptime = Sleeptime(
        agent_state, block_manager, passage_manager, message_manager,
        structured, enable=config.sleeptime_enable,
        frequency=config.sleeptime_agent_frequency)

    asyncio.run(_repl(supervisor, conversation,
                      io=io, renderer=renderer, sleeptime=sleeptime,
                      config=config))