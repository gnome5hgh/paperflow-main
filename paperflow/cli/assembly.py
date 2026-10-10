"""装配组合根：把配置、记忆、安全、意图、工具与终端装配成一个可运行的 REPL。

装配顺序与依赖方向见 `main()` 的 docstring；启动预检在 `bootstrap.py`。
"""
import asyncio
import sys
import uuid
from pathlib import Path

from rich.console import Console

from paperflow.cli.bootstrap import _ensure_services
from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent
from paperflow.core.agent import AgentRegistry
from paperflow.core.skills import merge_tools
from paperflow.core.skills import SkillRegistry, load_lock
from paperflow.core.mcp.services.bridge import collect_mcp_agent_tools
from paperflow.tools.skills.load_skill import LoadSkillTool
from paperflow.core.llm import LLMClient
from paperflow.core.security import (
    AuditMiddleware, WorkspacePolicyMiddleware,
    SecurityScanMiddleware, PolicyEngineMiddleware,
)
from paperflow.core.llm import StructuredOutput
from paperflow.core.memory.storage.database import MemoryDB
from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.tools.memory import set_memory_context, MemoryToolsContext
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.core.memory.services.consolidation import MemoryConsolidator
from paperflow.core.intent.services.jev import JevClient, JevUnavailable
from paperflow.core.intent.services.service import IntentService
from paperflow.core.intent.rules.taxonomy import TaxonomyError, load_taxonomy
from paperflow.terminal.confirm import ConfirmCenter, _make_confirm_callback
from paperflow.terminal.io import make_input_io
from paperflow.terminal.render import make_renderer
from paperflow.terminal.repl import _repl, _make_print_fn
from paperflow.terminal.repl import build_resume_replay


def _select_resume_session(agent_manager: AgentManager, io) -> str | None:
    """列出历史会话并让用户选择要恢复的会话（`--resume` 不带 id 时）。

    Args:
        agent_manager: 记忆服务层句柄（读 agent_state / messages 表）。
        io: 输入适配器（读用户选择）。

    Returns:
        选中的 session_id；取消（空输入）或无历史会话时返回 None。

    选择方式：输入菜单编号，或直接粘贴会话 id。非法输入返回 None 并打印原因。
    """
    sessions = agent_manager.list_sessions(limit=10)
    if not sessions:
        print("没有可恢复的历史会话。")
        return None
    print("历史会话（新→旧）：")
    for i, s in enumerate(sessions, 1):
        print(f"  {i}. [{s['created_at'][:19]}] {s['agent_id']}"
              f"（{s['message_count']} 条消息）{s['preview']}")
    try:
        raw = io.read("输入编号恢复（回车取消）：").strip()
    except (EOFError, KeyboardInterrupt):
        return None
    if not raw:
        return None
    if raw.isdigit() and 1 <= int(raw) <= len(sessions):
        return sessions[int(raw) - 1]["agent_id"]
    if any(s["agent_id"] == raw for s in sessions):
        return raw
    print(f"无效选择：{raw}")
    return None


def main(argv: list[str] | None = None) -> int | None:
    """装配全部依赖并启动 REPL。


    Returns:
        无参 REPL 路径返回 None（REPL 正常退出即成功）。


    命令行参数（argparse）：
        --help：用法。
        --resume [SESSION_ID]：恢复历史会话；不带 id 时列出历史会话供选择。
        --skip-bootstrap：跳过依赖服务启动预检（等价 PAPERFLOW_SKIP_BOOTSTRAP=1）。
    无参数行为与历史版本完全一致：装配后进入新会话 REPL。

    装配顺序（依赖关系）：
        1. 终端 IO 和渲染器（输入/输出适配）。
        2. 会话 ID（用于记忆服务键控；--resume 时复用已落盘会话）。
        3. 记忆服务层：DB → BlockManager → MessageManager → AgentManager。
        4. 意图编码器（云端实例）注入 MessageManager。
        5. AgentManager 回填到 MessageManager（用于读取 AgentState）。
        6. 创建 AgentState 和结构化输出。
        7. 设置记忆工具上下文。
        8. 构造安全中间件、意图管线。
        9. 构造 Supervisor Agent 和 MemoryConsolidator。
        10. 运行 REPL 主循环。

    关键依赖顺序：
        - AgentManager 依赖 BlockManager 和 MessageManager；MessageManager 需要 AgentManager 来获取 in-context 窗口，
          因此创建顺序为：先建 AgentManager，再回填 MessageManager.agent_manager。

    Args:
        argv: list[str] | None，命令行参数（None 时读 sys.argv）
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="paperflow",
        description="paperFlow 学术研究工作流助手（交互式 REPL，自然语言即命令）")
    parser.add_argument("--resume", nargs="?", const="", default=None,
                        metavar="SESSION_ID",
                        help="恢复历史会话；不带 id 则列出历史会话供选择")
    parser.add_argument("--skip-bootstrap", action="store_true",
                        help="跳过依赖服务（Milvus）启动预检")
    args = parser.parse_args(argv)

    config = PaperFlowConfig.from_env()
    # MCP 客户端平台：config.mcp_servers 非空才启动（后台循环 + 非阻塞预取）。
    # 工具注入发生在下方装配循环（与 skill merge 同缝）；管理器引用同时传给
    # _repl 供 /mcp 命令，退出时 shutdown。
    from paperflow.core.mcp.services.client import McpClientManager
    mcp_manager = McpClientManager(config.mcp_servers)
    if config.mcp_servers:
        mcp_manager.start()
    is_tty = sys.stdin.isatty()
    io = make_input_io(config)
    console = Console() if is_tty else None
    # 启动预检：依赖服务（Milvus）未起时自动 docker compose 拉起（仅 TTY，
    # 管道/CI 跳过）。软依赖：任何失败只产出警告不阻塞——服务缺席时 RAG/PDF 降级。
    service_warnings = _ensure_services(
        config, is_tty=is_tty, skip=args.skip_bootstrap,
        notify=(lambda msg: console.print(msg, style="dim")) if console else None)
    for w in service_warnings:
        (console.print(w, style="yellow") if console else print(w))
    # api_key 缺失提示：RAG 的云端嵌入未配 key 时只提示不阻断（检索退纯
    # BM25、无精排，降级路径由 retriever 自己消化）。
    if not config.rag.embedding.api_key:
        _msg = ("未配置云端嵌入 api_key（config.yaml rag.embedding 段）："
                "RAG 检索无稠密路与精排。"
                "注册 siliconflow.cn 获取（含实名认证）。")
        (console.print(_msg, style="yellow") if console else print(_msg))
    try:
        llm = LLMClient(config.llm)
    except RuntimeError as e:
        # 未配置 API key：用户语言的红字提示，不再是裸 traceback
        (console.print(f"[red]{e}[/red]") if console else print(f"错误：{e}"))
        sys.exit(1)
    # agents 插件目录：配置路径不存在时回退安装根（从非仓库目录启动也能找到插件；
    # 与 _find_compose_dir 的「cwd 优先、安装根回退」同一模式）
    agents_dir = (config.runtime.agents_dir if Path(config.runtime.agents_dir).is_dir()
                  else str(Path(__file__).resolve().parents[1] / "agents"))
    registry = AgentRegistry(agents_dir)

    # Skill 体系装配：单级扫描（<cwd>/.paperflow/skills/），skill 工具
    # 并入各 agent 工具表、load_skill 注入全部 agent（AgentConfig 为共享
    # 对象，此处就地修改即对后续所有 Agent 构造生效）。supervisor 的 skill 工具
    # 并入被 SkillRegistry.get_tools_for 代码级拒绝（权限最小化红线）。
    # lock 中 enabled=false 的 skill 扫描期跳过（enabledPlugins 语义）；lock
    # schema 版本不符时 load_lock 抛 ValueError——启动 fail-fast，与 SkillRegistry
    # 校验同哲学（不安全状态不进系统）。
    _pf_dir = Path.cwd() / ".paperflow"
    _skills_dir = _pf_dir / "skills"
    _disabled = {n for n, e in load_lock(_pf_dir).items() if not e.get("enabled", True)}
    skill_registry = SkillRegistry(
        str(_skills_dir) if _skills_dir.is_dir() else None, disabled=_disabled)
    # load_skill 无可见性门控（skill 对所有 agent 可见），也不持有父 Agent 引用，
    # 因此全进程共享一个实例即可被所有 agent 安全复用。
    _load_skill_tool = LoadSkillTool(skill_registry)
    for _agent_type in registry.list_agents():
        _cfg = registry.get_config(_agent_type)
        _cfg.tools = merge_tools(
            ("agent", _cfg.tools),
            # supervisor 的 skill 捆绑工具并入被 get_tools_for 代码级拒绝（权限最小化红线）
            ("skill", skill_registry.get_tools_for(_agent_type)),
            ("framework", [_load_skill_tool]),
            ("mcp", collect_mcp_agent_tools(_agent_type, config.mcp_servers, mcp_manager)),
        )

    # 终端装配：TTY → prompt_toolkit 输入 + rich Live 渲染；非 TTY（管道/CI/测试）→
    # FallbackIO + PlainBlock 降级。renderer 须在 supervisor 前构造——confirm_callback
    # （写/编辑确认 diff 预览）是 supervisor 构造参数。
    # root_agent_type 恒为 "supervisor"（根 agent 的 agent_type 见 supervisor 构造处）。
    renderer = make_renderer(
        _make_print_fn(console),
        "supervisor",
        is_tty=is_tty, console=console,
    )

    # 会话标识：本次进程启动即一个会话；--resume 时复用已落盘会话 id。
    # AgentManager.create_agent 的 agent_id 与 Agent.session_id 必须一致——
    # 记忆工具（SQL 按 agent_id 键控）与 MemoryConsolidator 都挂在它下面，三者对不上
    # 会各自读到空数据。
    memory_dir = Path(config.runtime.workspace) / "memory"
    db = MemoryDB(memory_dir / "memory.db")
    block_manager = GitEnabledBlockManager(db, memfs_dir=memory_dir)
    block_manager.migrate_legacy_labels()   # 旧 human/persona label 一次性迁移为 profile/assistant（幂等）
    block_manager.purge_removed_blocks()    # 已下线功能的块（历史清单）一次性清除（幂等）
    block_manager.ensure_default_blocks()   # 首启播种默认 profile/assistant 核心记忆块
    message_manager = MessageManager(db)
    agent_manager = AgentManager(db, block_manager, message_manager)

    resume_hint: str | None = None
    if args.resume is not None:
        if args.resume:
            # 带 id：会话必须存在，否则给出友好列表后退出
            try:
                agent_manager.get_agent(args.resume)
            except KeyError:
                print(f"未找到会话 {args.resume}。可运行 `paperflow --resume` 查看历史会话列表。")
                sys.exit(1)
            session_id = args.resume
        else:
            session_id = _select_resume_session(agent_manager, io)
            if session_id is None:
                sys.exit(0)
    else:
        session_id = uuid.uuid4().hex[:8]
        # 无参启动保持新会话；检测到历史会话时提示可恢复（不自动询问）
        last = agent_manager.list_sessions(limit=1)
        if last and last[0]["message_count"] > 0:
            preview = last[0]["preview"] or "（无预览）"
            resume_hint = f"检测到上次会话（{preview}），paperflow --resume 可恢复"

    # MessageManager 经 agent_manager 读 AgentState.message_ids（in-context 窗口），
    # 压缩后的摘要/尾部要跨轮回放——装配顺序上 agent_manager 后置，故在此回填。
    message_manager.agent_manager = agent_manager
    if args.resume is not None:
        agent_state = agent_manager.get_agent(session_id)
    else:
        agent_state = agent_manager.create_agent(session_id)

    # 会话恢复的历史回放：--resume 恢复的是模型上下文，屏幕上
    # 否则不留任何痕迹，用户会以为恢复失败。这里把**同一个** in-context 窗口投影成
    # 回放载荷（数据源与模型一致），由 _repl 在横幅之后渲染进滚动区。
    # 必须在此处（message_manager.agent_manager 回填之后）构建：否则
    # get_in_context_messages 读不到窗口，会降级成全量查询、与模型所见不一致。
    resume_replay = None
    if args.resume is not None and config.session.resume_replay:
        resume_replay = build_resume_replay(
            message_manager, session_id, limit=config.session.resume_replay_limit,
            created_at=(str(agent_state.created_at)[:16]
                        if agent_state.created_at else None))

    structured = StructuredOutput(llm)

    set_memory_context(MemoryToolsContext(
        agent_id=session_id,
        block_manager=block_manager,
        message_manager=message_manager,
    ))

    # 安全管道：四中间件（经验记忆中间件已移除——工具调用经验不再注入 prompt，
    # 改由 MemoryConsolidator 后台整合进核心记忆块）。
    middlewares = [
        # 审计目录从 workspace 派生：默认按 cwd 相对定位会让
        # PAPERFLOW_RUNTIME_WORKSPACE 重定向时审计仍写进仓库 .paperflow/security/audit，与其他会话的审计混写；
        # 且 cwd 下的 .paperflow/security/audit 落在 WorkspacePolicy 的 ws/security 保护约定之外)
        AuditMiddleware(audit_dir=str(Path(config.runtime.workspace) / "security" / "audit")),
        WorkspacePolicyMiddleware(workspace=config.runtime.workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk=config.runtime.max_risk),
    ]

    # 意图识别装配（可选预处理层）：关时整段跳过——不装载知识库、不构造适配器，
    # supervisor 走纯 ReAct。启用的前提是知识库能装载：类别缺条目、规则指向未知
    # 类别这类问题在这里就炸掉（fail-closed），不许跑到某一轮才静默走偏。
    # 意图识别装配（可选预处理层）：关时整段跳过——不装载知识库、不探测判定服务，
    # supervisor 走纯 ReAct。开启要过两道：① 知识库能装载（类别缺条目、规则指向未知
    # 类别这类问题在这里就炸掉，fail-closed）；② 判定服务可达（网关是托管 API，
    # 账户未绑卡一律 403）。**判定服务不可达则整套意图层不装配**——只有规则层能判的
    # 那一小部分不值得挂着一个「大部分请求没有提示」的半层。
    intent_service = None
    if config.intent.enabled:
        try:
            taxonomy = load_taxonomy()
        except TaxonomyError as exc:
            # 知识库坏了是配置/数据错误（类别与枚举对不上、规则写错），
            # 必须人去改——fail-closed 拒绝启动，但给一行能读懂的话而不是 traceback。
            _msg = f"意图识别已启用，但知识库装载失败：{exc}"
            (console.print(_msg, style="red") if console else print(_msg))
            sys.exit(1)
        judge = JevClient(config.intent.jev.base_url, config.intent.jev.api_key,
                          config.intent.jev.model,
                          timeout=config.intent.jev.timeout,
                          max_retries=config.intent.jev.max_retries,
                          zero_data_retention=config.intent.jev.zero_data_retention,
                          only_provider=config.intent.jev.only_provider)
        try:
            asyncio.run(judge.probe())
        except JevUnavailable as exc:
            _msg = (f"意图识别已启用，但判定服务不可用：{exc.reason}\n"
                    "  本轮起意图层不装配（supervisor 走纯 ReAct，行为与关闭意图一致）。"
                    "改好配置或恢复网络后重启生效。")
            (console.print(_msg, style="yellow") if console else print(_msg))
        else:
            intent_service = IntentService(
                taxonomy=taxonomy, judge=judge,
                history_messages=config.intent.history_messages)

    # 确认中心：确认的唯一消费者，跑在 REPL 主事件循环上（启动/收尾在
    # _repl 内）。confirm 回调经它跨线程桥接，弹框期间渲染抑制——并行多
    # agent 的确认框不再被其他 agent 的渲染事件盖掉。
    center = ConfirmCenter(io, renderer)

    supervisor = Agent(
        llm=llm, agent_registry=registry, agent_type="supervisor",
        skill_registry=skill_registry,
        memory=agent_state.memory,
        agent_manager=agent_manager, block_manager=block_manager,
        message_manager=message_manager,
        compaction=config.compaction,
        structured=structured,
        security_middleware=middlewares,
        intent_service=intent_service,
        confirm_callback=_make_confirm_callback(io, renderer, center),
        session_id=session_id,
    )
    consolidator = MemoryConsolidator(
        agent_state, block_manager, message_manager,
        structured, enable=config.memory.consolidation_enabled)

    try:
        asyncio.run(_repl(supervisor,
                          io=io, renderer=renderer, consolidator=consolidator,
                          config=config, resume_hint=resume_hint,
                          confirm_center=center, resume_replay=resume_replay,
                          mcp_manager=mcp_manager))
    finally:
        mcp_manager.shutdown()
