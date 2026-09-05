# paperflow/cli.py
"""CLI 装配组合根：装配全部依赖（LLM/记忆/安全/意图）并启动 REPL。

交互半区（REPL 循环、确认/提问回调、横幅）在 paperflow/terminal/repl.py——
本模块只负责组装对象图（装配顺序与依赖方向见 main() docstring），不承载
终端交互逻辑。启动预检（依赖服务自动拉起）见 paperflow/bootstrap.py。
"""
import asyncio
import sys
import uuid
from pathlib import Path

from rich.console import Console

from paperflow.bootstrap import ensure_services
from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent
from paperflow.core.agent_registry import AgentRegistry
from paperflow.core.llm import LLMClient
from paperflow.core.intent.conversation_state import ConversationState
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
from paperflow.terminal.io import make_input_io
from paperflow.terminal.render import make_renderer
from paperflow.terminal.repl import (
    _repl, _make_print_fn, _make_confirm_callback, _make_ask_callback)

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