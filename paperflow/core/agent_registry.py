# paperflow/core/agent_registry.py
"""
Agent 注册表 —— 扫描 agents/ 目录，统一加载配置和工具。

这是 paperFlow 插件体系的唯一入口。系统启动时扫描 ``agents/`` 下的每个子目录，
同时加载两份文件：

- ``SKILL.md``（YAML frontmatter + Markdown body）→ 配置元数据 + system prompt
- ``tools.py``（模块级 ``TOOLS`` 列表）→ Tool 实例

设计依据 ADR 0003：

- **单一注册表**：取代早期设计中分离的 SkillRegistry + SubAgentRegistry，
  一个类同时解析配置和导入工具，避免两套注册表之间的数据不同步
- **权限最小化**：``allowed_agents`` 限制哪些 agent_type 可以加载特权 Skill
  （Layer 1 由 Policy Engine 执行校验）
- **Spawn 控制**：``allowed_spawns`` 声明本 agent 能 spawn 哪些 SubAgent
  （Layer 4 由 SpawnSubAgentTool 执行校验）
"""

import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from paperflow.core.tool import RISK_LEVELS, SIDE_EFFECTS, Tool


@dataclass
class AgentConfig:
    """
    单个 Agent 的完整配置，由 AgentRegistry 从 agents/<name>/ 目录加载。

    字段来源::

        name            ← SKILL.md frontmatter "name" 或目录名
        description     ← SKILL.md frontmatter "description"
        system_prompt   ← SKILL.md 正文（frontmatter 后的 Markdown）
        allowed_agents  ← SKILL.md frontmatter "allowed_agents"
                          空列表 = 公开，任何 agent 可加载此 Skill 的 Tool
        allowed_spawns  ← SKILL.md frontmatter "allowed_spawns"
                          空列表 = 不能 spawn 任何 SubAgent
        tools           ← tools.py 模块级 TOOLS 列表
    """

    #: Agent 类型标识符，对应 agents/ 下的目录名（如 "search-paper"）
    name: str

    #: 简短描述，供 LLM 在 Supervisor 选择 spawn 目标时参考
    description: str = ""

    #: 注入 LLM system prompt 的完整文本，定义 Agent 的行为规范
    system_prompt: str = ""

    #: 特权控制：只有白名单中的 agent_type 可加载此 Agent 的 Tool（Layer 1 执行）
    allowed_agents: list[str] = field(default_factory=list)

    #: Spawn 权限：本 Agent 能 spawn 哪些 SubAgent（Layer 4 执行）
    allowed_spawns: list[str] = field(default_factory=list)

    #: 本 Agent 拥有的 Tool 实例列表，从 tools.py 的 TOOLS 列表加载
    tools: list[Tool] = field(default_factory=list)


class AgentRegistry:
    """
    扫描 agents/ 目录，同时加载配置和工具的唯一注册表。

    使用方式::

        registry = AgentRegistry("agents")
        config = registry.get_config("search-paper")
        print(config.system_prompt)   # 从 SKILL.md 正文加载
        print(config.tools)           # 从 tools.py TOOLS 列表加载

    扫描逻辑：
        遍历 ``agents_dir`` 下所有子目录
        → 跳过无 SKILL.md 的目录
        → 解析 YAML frontmatter + Markdown body
        → importlib 动态加载 tools.py，读取 TOOLS 列表
        → 组装 AgentConfig 存入内部字典
    """

    def __init__(self, agents_dir: str = "agents"):
        """
        :param agents_dir: Agent 插件根目录路径，默认为项目根下的 agents/
        """
        #: agent_type → AgentConfig 的映射字典
        self._agents: dict[str, AgentConfig] = {}
        self._discover(Path(agents_dir))

    def _discover(self, agents_dir: Path) -> None:
        """
        遍历 agents_dir 下所有子目录，发现并加载 Agent。

        每个子目录需包含 SKILL.md（配置+prompt），
        可选包含 tools.py（Tool 实例）。
        目录按名称排序以确保加载顺序可预测。
        """
        if not agents_dir.is_dir():
            return

        for agent_path in sorted(agents_dir.iterdir()):
            # 跳过非目录文件（如 .DS_Store）
            if not agent_path.is_dir():
                continue

            # SKILL.md 是 Agent 的必需文件
            skill_md = agent_path / "SKILL.md"
            if not skill_md.exists():
                continue

            # 解析 YAML frontmatter（元数据）+ Markdown body（system_prompt）
            meta, body = self._parse_skill_md(skill_md)

            # 目录名作为 agent_type；如 frontmatter 指定 name 则覆盖
            name = meta.get("name", agent_path.name)

            # importlib 动态加载 tools.py → 读取 TOOLS 列表
            tools = self._import_tools(agent_path / "tools.py")

            self._agents[name] = AgentConfig(
                name=name,
                description=meta.get("description", ""),
                # system_prompt 优先取 Markdown 正文，回退到 description
                system_prompt=body.strip() if body else meta.get("description", ""),
                allowed_agents=meta.get("allowed_agents", []),
                allowed_spawns=meta.get("allowed_spawns", []),
                tools=tools,
            )

    def _parse_skill_md(self, path: Path) -> tuple[dict, str]:
        """
        解析 SKILL.md 文件，分离 YAML frontmatter 和 Markdown body。

        SKILL.md 格式::

            ---
            name: search-paper
            description: 学术论文搜索
            allowed_agents: []
            allowed_spawns: []
            ---

            # 行为指导

            这里是 Markdown 正文，作为 system prompt 注入 LLM。

        :param path: SKILL.md 文件路径
        :returns: (frontmatter 字典, body 文本)
        """
        text = path.read_text(encoding="utf-8")

        # 匹配 ``---\n...\n---\n...`` 的正则（DOTALL 让 . 匹配换行符）
        frontmatter = {}
        body = text
        m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)", text, re.DOTALL)
        if m:
            # 安全加载 YAML（safe_load 只解析基本类型，不执行任意代码）
            frontmatter = yaml.safe_load(m.group(1)) or {}
            body = m.group(2).strip()
        return frontmatter, body

    def _import_tools(self, tools_path: Path) -> list[Tool]:
        """
        通过 importlib 动态加载 tools.py 并提取 TOOLS 列表。

        约定：每个 Agent 的 tools.py 模块级必须定义 ``TOOLS = [Tool(), ...]`` 列表。
        如果 tools.py 不存在，返回空列表（Agent 无可用 Tool）。

        .. note::

            使用 ``module_from_spec`` + ``exec_module`` 而非直接 import，
            避免模块插入 sys.modules 导致不同 Agent 的同名 tools.py 冲突。
            每次调用都会重新执行模块级代码（纯 Tool 实例化，开销极小）。

        :param tools_path: tools.py 文件路径
        :returns: Tool 实例列表
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
        """
        加载时校验 Tool 的安全元数据字段，非法值立即抛 ValueError。

        校验点（Layer 1 安全中间件的前置防线）：
        - ``risk_level`` ∈ RISK_LEVELS
        - ``side_effects`` 每个值 ∈ SIDE_EFFECTS
        - ``output_scan`` ∈ (None, "mark")

        :param tool: 待校验的 Tool 实例
        :raises ValueError: 任一字段值非法时抛出，携带工具名和合法值列表
        """
        if tool.risk_level not in RISK_LEVELS:
            raise ValueError(
                f"Tool '{tool.name}': 非法 risk_level '{tool.risk_level}'，"
                f"合法值: {sorted(RISK_LEVELS)}"
            )
        invalid_effects = [s for s in tool.side_effects if s not in SIDE_EFFECTS]
        if invalid_effects:
            raise ValueError(
                f"Tool '{tool.name}': 非法 side_effects: {invalid_effects}，"
                f"合法值: {sorted(SIDE_EFFECTS)}"
            )
        if tool.output_scan not in (None, "mark"):
            raise ValueError(
                f"Tool '{tool.name}': 非法 output_scan '{tool.output_scan}'，"
                f"合法值: None / 'mark'"
            )

    def get_config(self, agent_type: str) -> AgentConfig:
        """
        按 agent_type 返回完整配置（含 tools）。

        :param agent_type: Agent 类型标识符，如 "supervisor"、"search-paper"
        :returns: AgentConfig 实例
        :raises KeyError: 如果 agent_type 未在 agents/ 目录下注册
        """
        config = self._agents.get(agent_type)
        if config is None:
            raise KeyError(f"Unknown agent type: {agent_type}")
        return config

    def list_agents(self) -> list[str]:
        """
        返回所有已注册 agent_type 的列表。

        供 Supervisor 在 spawn 决策时参考可用 SubAgent 清单。
        """
        return list(self._agents.keys())
