# paperflow/core/agent/registry.py
"""
Agent 注册表 —— 扫描 agents/ 目录,统一加载配置和工具。

这是 paperFlow 插件体系的唯一入口。系统启动时扫描 ``agents/`` 下的每个子目录,
同时加载两份文件:

- ``AGENT.md``(YAML frontmatter + Markdown body)→ 配置元数据 + system prompt
- ``tools.py``(模块级 ``TOOLS`` 列表)→ Tool 实例

设计要点:

- **单一注册表**:一个类同时解析配置和导入工具,避免两套注册表数据不同步
- **权限最小化**:``allowed_agents`` 限制哪些 agent 类型可以加载特权 Agent 定义捆绑的 Tool
- **Spawn 控制**:``allowed_spawns`` 声明本 agent 能 spawn 哪些子 agent
"""

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

from paperflow.core.frontmatter import parse_frontmatter
from paperflow.core.tool import Tool, validate_tool


@dataclass
class AgentConfig:
    """单个 Agent 的完整配置，由 AgentRegistry 从 agents/<name>/ 目录加载。

    字段来源::

        name            ← AGENT.md frontmatter "name" 或目录名
        description     ← AGENT.md frontmatter "description"
        system_prompt   ← AGENT.md 正文（frontmatter 后的 Markdown）
        allowed_agents  ← AGENT.md frontmatter "allowed_agents"
                          空列表 = 公开，任何 agent 可加载其 Tool
        allowed_spawns  ← AGENT.md frontmatter "allowed_spawns"
                          空列表 = 不能 spawn 任何 SubAgent
        tools           ← tools.py 模块级 TOOLS 列表

    Attributes:
        name: str，Agent 类型标识（对应 agents/ 下目录名）
        description: str，简短描述（供 LLM 选择 spawn 目标时参考）
        system_prompt: str，注入 LLM 的 system prompt（AGENT.md 正文）
        allowed_agents: list[str]，可加载本 Agent 工具的白名单（空 = 公开）
        allowed_spawns: list[str]，本 Agent 能 spawn 的子 agent（空 = 不能 spawn）
        tools: list[Tool]，从 tools.py 的 TOOLS 加载的工具实例
    """

    #: Agent 类型标识符，对应 agents/ 下的目录名（如 "searcher"）
    name: str

    #: 简短描述，供 LLM 在 Supervisor 选择 spawn 目标时参考
    description: str = ""

    #: 注入 LLM system prompt 的完整文本，定义 Agent 的行为规范
    system_prompt: str = ""

    #: 特权控制:只有白名单中的 agent 类型可加载此 Agent 的工具(策略层执行)
    allowed_agents: list[str] = field(default_factory=list)

    #: Spawn 权限:本 Agent 能 spawn 哪些子 agent(spawn 工具运行时校验)
    allowed_spawns: list[str] = field(default_factory=list)

    #: 本 Agent 拥有的 Tool 实例列表，从 tools.py 的 TOOLS 列表加载
    tools: list[Tool] = field(default_factory=list)


class AgentRegistry:
    """扫描 agents/ 目录，同时加载配置和工具的唯一注册表。

    使用方式::

        registry = AgentRegistry("agents")
        config = registry.get_config("searcher")
        print(config.system_prompt)   # 从 AGENT.md 正文加载
        print(config.tools)           # 从 tools.py TOOLS 列表加载

    扫描逻辑：
        遍历 ``agents_dir`` 下所有子目录
        → 跳过无 AGENT.md 的目录
        → 解析 YAML frontmatter + Markdown body
        → importlib 动态加载 tools.py，读取 TOOLS 列表
        → 组装 AgentConfig 存入内部字典

    Attributes:
        _agents: dict[str, AgentConfig]，agent_type → 配置的映射
    """

    def __init__(self, agents_dir: str = "agents"):
        """
        构造即触发全量扫描（_discover 遍历目录 + 动态导入 tools.py）。

        Args:
            agents_dir: Agent 插件根目录路径，默认为项目根下的 agents/

        Raises:
            ValueError: 任一 Tool 的 risk_level / side_effects / output_scan 非法时抛出，
                终止构造，防止不安全配置进入系统。

        Note: 本构造有副作用（动态导入多个 tools.py），进程内应只构造一次（由装配层持有并传给所有 Agent）。
        """
        #: agent_type → AgentConfig 的映射字典（key 为 agent 类型，值为对应的AgentConfig）
        self._agents: dict[str, AgentConfig] = {}
        self._discover(Path(agents_dir))

    def _discover(self, agents_dir: Path) -> None:
        """
        遍历 agents_dir 下所有子目录，发现并加载 Agent。

        每个子目录需包含 AGENT.md（配置 + prompt），
        可选包含 tools.py（Tool 实例）。
        目录按名称排序以确保加载顺序可预测。

        Args:
            agents_dir: 要扫描的根目录路径（Path 对象）

        Note: 若目录不存在或非目录，则直接返回（不做任何加载）。
        """
        if not agents_dir.is_dir():
            return

        # 按目录名排序遍历，保证不同运行环境加载顺序一致（便于调试和缓存）
        for agent_path in sorted(agents_dir.iterdir()):
            # 跳过非目录文件（如 .DS_Store）
            if not agent_path.is_dir():
                continue

            # AGENT.md 是 Agent 的必需文件，缺少则跳过该目录
            agent_md = agent_path / "AGENT.md"
            if not agent_md.exists():
                continue

            # 解析 YAML frontmatter（元数据）+ Markdown body（system_prompt）
            meta, body = self._parse_agent_md(agent_md)

            # 目录名作为 agent_type；如 frontmatter 指定 name 则覆盖
            name = meta.get("name", agent_path.name)

            # importlib 动态加载 tools.py → 读取 TOOLS 列表
            tools = self._import_tools(agent_path / "tools.py")

            # 组装配置并存入映射字典
            self._agents[name] = AgentConfig(
                name=name,
                description=meta.get("description", ""),
                # system_prompt 优先取 Markdown 正文，回退到 description
                system_prompt=body.strip() if body else meta.get("description", ""),
                allowed_agents=meta.get("allowed_agents", []),
                allowed_spawns=meta.get("allowed_spawns", []),
                tools=tools,
            )

    def _parse_agent_md(self, path: Path) -> tuple[dict, str]:
        """
        解析 AGENT.md 文件，分离 YAML frontmatter 和 Markdown body（解析实现在 core/frontmatter.py 共享）。

        AGENT.md 格式::

            ---
            name: searcher
            description: 学术论文搜索
            allowed_agents: []
            allowed_spawns: []
            ---

            # 行为指导

            这里是 Markdown 正文，作为 system prompt 注入 LLM。

        Args:
            path: AGENT.md 文件路径

        Returns:
            (frontmatter 字典, body 文本)

        Note: 若文件开头没有 `---` 标记，则 frontmatter 为空字典，整个文件作为 body。
        """
        return parse_frontmatter(path.read_text(encoding="utf-8"))

    def _import_tools(self, tools_path: Path) -> list[Tool]:
        """
        通过 importlib 动态加载 tools.py 并提取 TOOLS 列表。

        约定：每个 Agent 的 tools.py 模块级必须定义 ``TOOLS = [Tool(), ...]`` 列表。
        如果 tools.py 不存在，返回空列表（Agent 无可用 Tool）。

        .. note::

            使用 ``module_from_spec`` + ``exec_module`` 而非直接 import，
            避免模块插入 sys.modules 导致不同 Agent 的同名 tools.py 冲突。
            每次调用都会重新执行模块级代码（纯 Tool 实例化，开销极小）。

        Args:
            tools_path: tools.py 文件路径

        Returns:
            Tool 实例列表

        Raises:
            ValueError: 如果某个 Tool 的安全元数据（risk_level / side_effects / output_scan）非法，会立即抛出，终止该 Agent 的加载。

        """
        if not tools_path.exists():
            return []

        # 用目录名生成唯一模块名，防止两次同名加载覆盖
        spec = importlib.util.spec_from_file_location(
            f"agent_tools_{tools_path.parent.name}", str(tools_path)
        )
        module = importlib.util.module_from_spec(spec)
        # exec_module 在模块的独立命名空间中执行代码
        spec.loader.exec_module(module)

        # 约定：TOOLS 是模块级变量，类型为 list[Tool]
        tools = getattr(module, "TOOLS", [])

        # 加载时校验每个 Tool 的安全元数据，非法值立即抛 ValueError
        for tool in tools:
            self._validate_tool(tool)
        return tools

    @staticmethod
    def _validate_tool(tool) -> None:
        """委托模块级 validate_tool（与 SkillRegistry 共用同一份校验）。

        Args:
            tool: Tool，待校验的工具实例（安全元数据合法性）
        """
        validate_tool(tool)

    def get_config(self, agent_type: str) -> AgentConfig:
        """
        按 agent_type 返回完整配置（含 tools）。

        Args:
            agent_type: Agent 类型标识符，如 "supervisor"、"searcher"

        Returns:
            AgentConfig 实例

        Raises:
            KeyError: 如果 agent_type 未在 agents/ 目录下注册

        Note: 若 agent_type 存在但对应的 tools.py 中 Tool 校验失败，构造时即已抛出异常，
               因此不会出现配置不完整的情况。
        """
        config = self._agents.get(agent_type)
        if config is None:
            raise KeyError(f"Unknown agent type: {agent_type}")
        return config

    def list_agents(self) -> list[str]:
        """
        返回所有已注册 agent_type 的列表。

        供 Supervisor 在 spawn 决策时参考可用 SubAgent 清单。

        Returns:
            按加载顺序（即目录名排序）排列的 agent_type 名字列表。

        """
        return list(self._agents.keys())

    def agents_block(self, exclude: set[str] | None = None) -> str:
        """渲染 <available_agents> 清单块（供派发方按能力选型）。

        Args:
            exclude: 不列入清单的 agent 类型（如派发方自身）。

        Returns:
            清单文本；无可列条目时返回空串（调用方据此整块省略）。
        """
        skip = exclude or set()
        lines = [f"- {name}: {self.get_config(name).description}"
                 for name in self.list_agents() if name not in skip]
        if not lines:
            return ""
        return ("<available_agents>\n"
                "可派发的子 agent（按能力选择，说明即其职责与边界）：\n"
                + "\n".join(lines) + "\n</available_agents>")
