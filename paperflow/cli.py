# paperflow/cli.py
"""CLI 装配组合根 + 启动预检：装配全部依赖（LLM/记忆/安全/意图）并启动 REPL。

交互半区（REPL 循环、确认/提问回调、横幅）在 paperflow/terminal/repl.py——
本模块负责两件事：装配前启动预检（依赖服务自动拉起，见下方预检段）与组装
对象图（装配顺序与依赖方向见 main() docstring），不承载终端交互逻辑。
"""
import asyncio
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

# 必须在任何可能拉起 grpc 的导入（paperflow.rag → pymilvus）之前设置——gRPC C 核心在
# 初始化时读取。不设时 gRPC 打 INFO 级日志：MCP server 子进程 fork 时，pymilvus
# 已建连的轮询 fd 残留会让每个子进程打一行 "FD from fork parent still in poll
# list"（ev_poll_posix.cc），纯噪音。setdefault 不覆盖用户显式配置。
os.environ.setdefault("GRPC_VERBOSITY", "ERROR")

from rich.console import Console

from paperflow.config import PaperFlowConfig
from paperflow.core.agent import Agent
from paperflow.core.agent import AgentRegistry
from paperflow.core.skills import merge_tools
from paperflow.core.skills import SkillRegistry
from paperflow.core.mcp.bridge import collect_mcp_agent_tools
from paperflow.tools.skills.load_skill import LoadSkillTool
from paperflow.core.llm import LLMClient
from paperflow.core.intent.conversation_state import ConversationState
from paperflow.core.security import (
    AuditMiddleware, WorkspacePolicyMiddleware,
    SecurityScanMiddleware, PolicyEngineMiddleware,
)
from paperflow.core.llm import StructuredOutput
from paperflow.core.memory.orm.database import MemoryDB
from paperflow.core.memory.services.block_manager import GitEnabledBlockManager
from paperflow.core.memory.services.message_manager import MessageManager
from paperflow.tools.memory import set_memory_context, MemoryToolsContext
from paperflow.core.memory.services.title_extractor import TitleExtractor
from paperflow.core.memory.services.agent_manager import AgentManager
from paperflow.core.memory.sleeptime import Sleeptime
from paperflow.core.intent.pipeline import IntentPipeline
from paperflow.core.intent.routing.router import HybridRouter
from paperflow.rag.encoders.embedder import SbertEmbedder, resolve_model_dir
from paperflow.rag.parsers.grobid_client import GrobidClient
from paperflow.core.intent.routing.route_loader import load_routes
from paperflow.terminal.io import make_input_io
from paperflow.terminal.render import make_renderer
from paperflow.terminal.repl import (
    _repl, _make_print_fn, _make_confirm_callback, _make_ask_callback)
from paperflow.terminal.resume import build_resume_replay

#: 模块级 embedder 单例：千问嵌入模型首次调用才加载（sentence-transformers 导入数秒），
#: 进程内只加载一次。RAG/意图管线/记忆服务共享同一实例——各自 new 一个会让同一
#: 模型权重被反复加载，启动变慢且占内存。
_embedder: "SbertEmbedder | None" = None


def _rag_embedder(config: PaperFlowConfig) -> "SbertEmbedder":
    """
    懒加载共享的千问嵌入模型单例。

    用途：
        - 意图管线的稠密路由（HybridRouter）
        - MessageManager 的可选 embedder 参数（该类检索为纯 SQL LIKE，当前未使用）
    所有组件共享同一实例，避免重复加载模型权重（首次加载需数秒，且占用内存）。

    Args:
        config: 全局配置，包含 workspace 和 embed_model 名称。

    Returns:
        SbertEmbedder: 共享的嵌入模型实例。

    Notes:
        - 模型路径优先本地：resolve_model_dir 在 workspace/models/<name> 查找，
          若不存在则回退 HuggingFace 缓存。
        - 该函数在进程生命周期内只加载一次。
    """
    global _embedder
    if _embedder is None:
        _embedder = SbertEmbedder(
            model_name=resolve_model_dir(config.workspace, config.embed_model))
    return _embedder


# ── 启动预检（bootstrap）─────────────────────────────────────────────────────
# REPL 装配前调用 _ensure_services：对依赖服务端点做端口连通探测（socket，秒级）
# → 不可达则定位 docker-compose.yml 执行 `docker compose up -d` 并轮询至健康。
# 软依赖语义：任何失败（无 docker / compose 文件缺失 / compose 失败 / 等待超时）
# 都只产出警告文本，不抛异常、不阻塞启动——服务缺席时 RAG 检索与 PDF 解析自动
# 降级（降级逻辑在 rag/vision 层）。警告与进度文本经返回值/notify 回调交由
# main() 呈现，本段自身不打印。跳过条件：非 TTY（管道/CI/测试）或环境变量
# PAPERFLOW_SKIP_BOOTSTRAP=1——静默返回，连端口探测都不执行。

#: 等待服务健康的总时长：覆盖 Milvus healthcheck 的 start_period 90s + 余量
_WAIT_TIMEOUT_S = 150.0
_POLL_INTERVAL_S = 2.0       # 端口轮询间隔
_PROBE_TIMEOUT_S = 1.0       # 单次端口连通探测超时
_COMPOSE_TIMEOUT_S = 600.0   # docker compose up -d 上限（首启可能拉镜像）

_DEGRADE_NOTE = "RAG/PDF 解析功能降级，REPL 仍可正常使用"
_NO_DOCKER_WARN = f"未检测到 docker，无法自动拉起依赖服务（Milvus/GROBID）；{_DEGRADE_NOTE}"
_NO_COMPOSE_WARN = f"未找到 docker-compose.yml（当前目录与安装目录均无），无法自动拉起依赖服务；{_DEGRADE_NOTE}"
_GROBID_RUNBOOK_HINT = "若为首次启动，需先初始化 grobid-home，见 docs/测试指南/问题排查手册.md"


def _host_port(url: str) -> tuple[str, int]:
    """从服务 URL 提取 (host, port)；未显式写端口时按 http/https 语义补全。"""
    parsed = urlparse(url)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port


def _port_open(host: str, port: int, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    """TCP 连通探测：不假设 HTTP 健康路径，端口能建立连接即视为服务在。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_compose_dir() -> Path | None:
    """定位 docker-compose.yml 所在目录：优先当前工作目录，回退安装根
    （editable 安装后在任意目录启动也能找到仓库内的 compose 文件）。"""
    for base in (Path.cwd(), Path(__file__).resolve().parents[1]):
        if any((base / name).is_file()
               for name in ("docker-compose.yml", "docker-compose.yaml")):
            return base
    return None


def _compose_up(compose_dir: Path) -> str | None:
    """执行 docker compose up -d。成功返回 None；失败返回带原因的描述
    （取 stderr 尾部几行——compose 的报错信息都在输出末尾）。"""
    try:
        proc = subprocess.run(
            ["docker", "compose", "up", "-d"],
            cwd=compose_dir, capture_output=True, text=True,
            timeout=_COMPOSE_TIMEOUT_S)
    except OSError as e:
        return f"docker 命令无法执行（不存在或不可用）：{e}"
    except subprocess.TimeoutExpired:
        return f"docker compose up -d 超时（{_COMPOSE_TIMEOUT_S:.0f}s）"
    if proc.returncode != 0:
        output = (proc.stderr or proc.stdout or "").strip().splitlines()
        if not output:
            output = ["无错误输出"]
        return "docker compose up -d 失败：" + "；".join(output[-3:])
    return None


def _wait_healthy(endpoints: list[tuple[str, str, int]],
                  timeout_s: float, poll_interval_s: float
                  ) -> list[tuple[str, str, int]]:
    """轮询直至全部端口可达或超时，返回仍未就绪的 (名称, host, port) 列表。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        pending = [(n, h, p) for n, h, p in endpoints if not _port_open(h, p)]
        if not pending:
            return []
        time.sleep(poll_interval_s)
    return [(n, h, p) for n, h, p in endpoints if not _port_open(h, p)]


def _probe_app_layer(endpoints: list[tuple[str, str, int]]) -> list[str]:
    """端口可达之后的应用层探活（真实使用测试 P1-4/P3-6 的应用侧互补）。

    此前 bootstrap 只做 TCP 连通探测——容器「端口开了随即 Exited(1)」（etcd TSO
    超时崩溃）与「半健康栈」都能通过预检，RAG 静默降级 3.5 小时无人知晓。
    Milvus 用 pymilvus 语义级连接（list_collections），GROBID 用 /api/isalive。
    任何异常只产出警告、绝不抛出——软依赖语义不变。

    Returns:
        警告文本列表（空 = 应用层全部健康）。
    """
    warnings: list[str] = []
    for name, host, port in endpoints:
        try:
            if name == "Milvus":
                from pymilvus import MilvusClient
                client = MilvusClient(uri=f"http://{host}:{port}")
                client.list_collections()
                client.close()
            elif name == "GROBID":
                import httpx
                r = httpx.get(f"http://{host}:{port}/api/isalive", timeout=5.0)
                if r.status_code != 200 or r.text.strip().lower() != "true":
                    raise RuntimeError(f"isalive 返回 {r.status_code}: {r.text[:50]}")
        except Exception as e:
            warnings.append(
                f"{name} 端口可达但应用层探活失败（{e}）——服务可能已中途崩溃，"
                f"RAG/PDF 解析将降级。建议：docker compose restart 后重启 paperflow；"
                f"{_DEGRADE_NOTE}")
    return warnings


def _ensure_services(config: PaperFlowConfig, *, is_tty: bool, notify=None,
                     skip: bool = False,
                     wait_timeout_s: float = _WAIT_TIMEOUT_S,
                     poll_interval_s: float = _POLL_INTERVAL_S) -> list[str]:
    """
    启动预检：确保 Docker 依赖服务（Milvus/GROBID）就绪，未起则自动拉起。

    Args:
        config: 全局配置（读 milvus_uri / grobid_endpoint 两个端点）。
        is_tty: 是否交互终端——False（管道/CI/测试）直接跳过。
        skip: 显式跳过开关（--skip-bootstrap flag，与环境变量等价）。
        notify: 进度回调（str → None），拉起/等待阶段逐条调用；None 静默。
        wait_timeout_s: 等待服务健康的总时长（默认覆盖 Milvus start_period 90s）。
        poll_interval_s: 端口轮询间隔。

    Returns:
        警告文本列表（空 = 服务全部就绪或预检被跳过）。调用方负责呈现；
        本函数绝不抛异常——软依赖缺席不应阻塞 REPL 启动。
    """
    if not is_tty or skip or os.environ.get("PAPERFLOW_SKIP_BOOTSTRAP") == "1":
        return []

    endpoints = [
        ("Milvus", *_host_port(config.milvus_uri)),
        ("GROBID", *_host_port(config.grobid_endpoint)),
    ]
    if all(_port_open(h, p) for _, h, p in endpoints):
        return _probe_app_layer(endpoints)      # 端口在 → 应用层语义健康再确认

    if shutil.which("docker") is None:
        return [_NO_DOCKER_WARN]
    compose_dir = _find_compose_dir()
    if compose_dir is None:
        return [_NO_COMPOSE_WARN]
    if notify:
        notify("依赖服务未就绪，正在拉起（docker compose up -d，首次启动较慢）…")
    err = _compose_up(compose_dir)
    if err is not None:
        return [f"{err}；{_DEGRADE_NOTE}"]
    if notify:
        notify(f"等待服务健康（最长 {wait_timeout_s:.0f}s）…")

    warnings = []
    not_ready = _wait_healthy(endpoints, wait_timeout_s, poll_interval_s)
    for name, host, port in not_ready:
        w = f"{name} 服务未在 {wait_timeout_s:.0f}s 内就绪（{host}:{port}）；{_DEGRADE_NOTE}"
        if name == "GROBID":
            w += f"；{_GROBID_RUNBOOK_HINT}"
        warnings.append(w)
    # 端口就绪的子集再做应用层探活（未就绪的不重复报）
    ready = [(n, h, p) for n, h, p in endpoints if (n, h, p) not in not_ready]
    warnings.extend(_probe_app_layer(ready))
    return warnings


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
    """
    装配全部依赖并启动 REPL。

    :returns: skill 子命令（install/list/uninstall）返回退出码 int；--version 与
        无参 REPL 路径返回 None（REPL 正常退出即成功）。

    命令行参数（argparse，P2-5）：
        --help / --version：用法与版本。
        --resume [SESSION_ID]：恢复历史会话；不带 id 时列出历史会话供选择。
        --skip-bootstrap：跳过依赖服务启动预检（等价 PAPERFLOW_SKIP_BOOTSTRAP=1）。
        skill install/list/uninstall：skill 安装管理子命令（准入通道见paperflow/core/skills/install.py）；
        分发后短路返回，返回值即退出码，不进入下方 REPL 装配。
    无参数行为与历史版本完全一致：装配后进入新会话 REPL。

    装配顺序（依赖关系）：
        1. 终端 IO 和渲染器（输入/输出适配）。
        2. 会话 ID（用于记忆服务键控；--resume 时复用已落盘会话）。
        3. 记忆服务层：DB → BlockManager → MessageManager → AgentManager。
        4. 嵌入模型（单例）注入 MessageManager。
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
    import argparse

    parser = argparse.ArgumentParser(
        prog="paperflow",
        description="paperFlow 学术研究工作流助手（交互式 REPL，自然语言即命令）")
    parser.add_argument("--version", action="store_true",
                        help="显示版本号并退出")
    parser.add_argument("--resume", nargs="?", const="", default=None,
                        metavar="SESSION_ID",
                        help="恢复历史会话；不带 id 则列出历史会话供选择")
    parser.add_argument("--skip-bootstrap", action="store_true",
                        help="跳过依赖服务（Milvus/GROBID）启动预检")
    # skill 管理子命令（Task 9）：install/list/uninstall——纯文件操作 + 本地扫描，
    # 不依赖任何服务；分发在 --version 之后、装配之前短路返回（返回值即退出码）。
    sub = parser.add_subparsers(dest="command")
    skill_parser = sub.add_parser("skill", help="skill 安装管理（install/list/uninstall）")
    skill_action = skill_parser.add_subparsers(dest="skill_action", required=True)
    skill_inst = skill_action.add_parser("install", help="准入安装：本地目录 | git URL | zip/tar")
    skill_inst.add_argument("source")
    skill_inst.add_argument("-y", "--yes", action="store_true", help="跳过确认（仅纯指令 skill）")
    skill_inst.add_argument("--allow-code", action="store_true",
                            help="允许捆绑 tools.py 的 skill（安装前必须人工审读代码）")
    skill_action.add_parser("list", help="列出内置与已装 skill")
    skill_uni = skill_action.add_parser("uninstall", help="卸载 manifest 登记的 skill")
    skill_uni.add_argument("name")
    args = parser.parse_args(argv)

    if args.version:
        from importlib.metadata import PackageNotFoundError, version
        try:
            print(version("paperflow"))
        except PackageNotFoundError:
            print("unknown（开发环境：见 pyproject.toml）")
        return

    # skill 子命令分发：不启服务、不建 LLM、不进 REPL——skill 管理不需要任何服务。
    if args.command == "skill":
        from paperflow.core.skills import (
            install_skill, list_skills_command, uninstall_skill)
        config = PaperFlowConfig.from_env()
        workspace = Path(config.workspace)
        builtin_skills_dir = Path(__file__).resolve().parents[1] / "skills"
        if args.skill_action == "install":
            return install_skill(args.source, workspace,
                                 assume_yes=args.yes, allow_code=args.allow_code)
        if args.skill_action == "list":
            return list_skills_command(
                str(builtin_skills_dir) if builtin_skills_dir.is_dir() else None, workspace)
        if args.skill_action == "uninstall":
            return uninstall_skill(args.name, workspace)

    config = PaperFlowConfig.from_env()
    # MCP 客户端平台：config.mcp_servers 非空才启动（后台循环 + 非阻塞预取）。
    # 工具注入发生在下方装配循环（与 skill merge 同缝）；管理器引用同时传给
    # _repl 供 /mcp 命令，退出时 shutdown。
    from paperflow.core.mcp.client import McpClientManager
    mcp_manager = McpClientManager(config.mcp_servers)
    if config.mcp_servers:
        mcp_manager.start()
    is_tty = sys.stdin.isatty()
    io = make_input_io(config)
    console = Console() if is_tty else None
    # 启动预检：依赖服务（Milvus/GROBID）未起时自动 docker compose 拉起（仅 TTY，
    # 管道/CI 跳过）。软依赖：任何失败只产出警告不阻塞——服务缺席时 RAG/PDF 降级。
    service_warnings = _ensure_services(
        config, is_tty=is_tty, skip=args.skip_bootstrap,
        notify=(lambda msg: console.print(msg, style="dim")) if console else None)
    for w in service_warnings:
        (console.print(w, style="yellow") if console else print(w))
    try:
        llm = LLMClient(config.llm)
    except RuntimeError as e:
        # 未配置 API key（P2-4）：用户语言的红字提示，不再是裸 traceback
        (console.print(f"[red]{e}[/red]") if console else print(f"错误：{e}"))
        sys.exit(1)
    # agents 插件目录：配置路径不存在时回退安装根（从非仓库目录启动也能找到插件；
    # 与 _find_compose_dir 的「cwd 优先、安装根回退」同一模式）
    agents_dir = (config.agents_dir if Path(config.agents_dir).is_dir()
                  else str(Path(__file__).resolve().parents[1] / "agents"))
    registry = AgentRegistry(agents_dir)

    # Skill 体系装配：两级扫描（包内 skills/ + <workspace>/skills/），skill 工具
    # 并入各 agent 工具表、load_skill 注入全部 agent（AgentConfig 为共享
    # 对象，此处就地修改即对后续所有 Agent 构造生效）。supervisor 的 skill 工具
    # 并入被 SkillRegistry.get_tools_for 代码级拒绝（权限最小化红线）。
    builtin_skills_dir = Path(__file__).resolve().parents[1] / "skills"
    skill_registry = SkillRegistry(
        builtin_dir=str(builtin_skills_dir) if builtin_skills_dir.is_dir() else None,
        workspace_dir=str(Path(config.workspace) / "skills"),
    )
    for _agent_type in registry.list_agents():
        _cfg = registry.get_config(_agent_type)
        # LoadSkillTool 声明 needs_parent=True：Agent.__init__ 构造期即
        # attach_agent(self) 回写 _parent。共享单个实例会被最后构造的 Agent 覆盖
        # _parent——spawn 出子 agent 后 supervisor 门控读到的 agent_type 变成子
        # agent，可见性双向失效（该拒的放行、该放的拒）。因此每个 agent type 一个
        # 独立实例；同 type 的并发子 agent 共享同一实例是良性的——可见性门控只依赖
        # agent_type，不依赖每实例状态（_parent 在构造后不再变更）。
        _load_skill_tool = LoadSkillTool(skill_registry)
        _cfg.tools = merge_tools(
            ("agent", _cfg.tools),
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
    # 记忆工具（SQL 按 agent_id 键控）与 Sleeptime 都挂在它下面，三者对不上
    # 会各自读到空数据。
    memory_dir = Path(config.workspace) / "memory"
    db = MemoryDB(memory_dir / "memory.db")
    block_manager = GitEnabledBlockManager(db, memfs_dir=memory_dir)
    block_manager.migrate_legacy_labels()   # 旧 human/persona label 一次性迁移为 profile/assistant（幂等）
    block_manager.ensure_default_blocks()   # 首启播种默认 profile/assistant 核心记忆块
    embedder = _rag_embedder(config)
    message_manager = MessageManager(db, embedder=embedder)
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

    # 会话恢复的历史回放（P2-3 观感补齐）：--resume 恢复的是模型上下文，屏幕上
    # 否则不留任何痕迹，用户会以为恢复失败。这里把**同一个** in-context 窗口投影成
    # 回放载荷（数据源与模型一致），由 _repl 在横幅之后渲染进滚动区。
    # 必须在此处（message_manager.agent_manager 回填之后）构建：否则
    # get_in_context_messages 读不到窗口，会降级成全量查询、与模型所见不一致。
    resume_replay = None
    if args.resume is not None and config.resume_replay:
        resume_replay = build_resume_replay(
            message_manager, session_id, limit=config.resume_replay_limit,
            created_at=(str(agent_state.created_at)[:16]
                        if agent_state.created_at else None))

    structured = StructuredOutput(llm)

    # extract_title 工具的标题提取器注入记忆工具运行时上下文（LLM 层走
    # StructuredOutput 真实接线）。GROBID 层用 config.grobid_endpoint 装配：
    # extract_title 走本地 REST header 接口，不可达或解析失败时返回 None，
    # 自动落到 LLM 层兜底。
    set_memory_context(MemoryToolsContext(
        agent_id=session_id,
        block_manager=block_manager,
        message_manager=message_manager,
        title_extractor=TitleExtractor(grobid=GrobidClient(config.grobid_endpoint),
                                       llm=structured),
    ))

    # RAG 检索工具的对话历史提供者（query 改写 condense 用，spec 2026-10-04）：
    # 只取 in-context 窗口内的 user/assistant 消息尾部 6 条——窗口投影与模型
    # 所见一致（压缩后摘要也在窗口内，可理解长程指代），tool 消息不进改写。
    from paperflow.tools.rag.runtime_context import set_rag_context, RagToolsContext

    def _recent_chat_history() -> list:
        msgs = message_manager.get_in_context_messages(session_id)
        return [m for m in msgs if m.role in ("user", "assistant")][-6:]

    set_rag_context(RagToolsContext(history_provider=_recent_chat_history))

    # 安全管道：四中间件（经验记忆中间件已移除——工具调用经验不再注入 prompt，
    # 改由 Sleeptime 后台整合进核心记忆块）。
    middlewares = [
        # 审计目录从 workspace 派生（真实会话复验发现：默认 cwd 相对导致
        # PAPERFLOW_WORKSPACE 重定向时审计仍写进仓库 data/audit，与真实会话混写；
        # 且 cwd 下的 data/audit 在 WorkspacePolicy 的 ws/audit 保护约定之外）
        AuditMiddleware(audit_dir=str(Path(config.workspace) / "audit")),
        WorkspacePolicyMiddleware(workspace=config.workspace),
        SecurityScanMiddleware(),
        PolicyEngineMiddleware(max_risk=config.max_risk),
    ]

    # 意图管线:真实混合路由器 + LLM 兜底。千问 0.6B 小模型经 _rag_embedder 共享单例
    # (首次加载需几秒,与记忆服务同模型同实例,不重复加载);各意图阈值已由标定脚本
    # 写回 routes.yaml——这里只读已标定阈值,不做训练或阈值搜索。alpha 是稠密/稀疏
    # 信号的融合权重,与标定脚本保持一致。模型路径本地优先
    # (resolve_model_dir:data/models/<name>,否则回退 HF 名)。
    router = HybridRouter(
        encoder=embedder,
        routes=load_routes(), alpha=0.5)
    pipeline = IntentPipeline(router=router, structured=structured)

    conversation = ConversationState()

    # 确认中心：确认/提问的唯一消费者，跑在 REPL 主事件循环上（启动/收尾在
    # _repl 内）。confirm/ask 回调经它跨线程桥接，弹框期间渲染抑制——并行多
    # agent 的确认框不再被其他 agent 的渲染事件盖掉（真实使用测试 P0-1/P0-3）。
    from paperflow.terminal.confirm_center import ConfirmCenter
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
        intent_enabled=True, intent_pipeline=pipeline, conversation=conversation,
        confirm_callback=_make_confirm_callback(io, renderer, center),
        ask_user_callback=message_manager.make_ask_recorder(_make_ask_callback(io, renderer, center),
                                                            session_id),
        session_id=session_id,
    )
    sleeptime = Sleeptime(
        agent_state, block_manager, message_manager,
        structured, enable=config.sleeptime_enable,
        frequency=config.sleeptime_agent_frequency)

    try:
        asyncio.run(_repl(supervisor, conversation,
                          io=io, renderer=renderer, sleeptime=sleeptime,
                          config=config, resume_hint=resume_hint,
                          confirm_center=center, resume_replay=resume_replay,
                          mcp_manager=mcp_manager))
    finally:
        mcp_manager.shutdown()