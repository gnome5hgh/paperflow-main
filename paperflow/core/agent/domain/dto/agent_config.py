"""Agent 插件配置：`AgentRegistry` 扫描 `agents/<name>/` 的产物。"""
from dataclasses import dataclass, field

from paperflow.core.tool import Tool


@dataclass
class AgentConfig:
    """单个 Agent 的完整配置，由 AgentRegistry 从 agents/<name>/ 目录加载。

    字段来源::

        name            ← AGENT.md frontmatter "name" 或目录名
        description     ← AGENT.md frontmatter "description"
        system_prompt   ← AGENT.md 正文（frontmatter 后的 Markdown）
        allowed_spawns  ← AGENT.md frontmatter "allowed_spawns"
                          空列表 = 不能 spawn 任何 SubAgent
        tools           ← tools.py 模块级 TOOLS 列表

    Attributes:
        name: str，Agent 类型标识（对应 agents/ 下目录名）
        description: str，简短描述（供 LLM 选择 spawn 目标时参考）
        system_prompt: str，注入 LLM 的 system prompt（AGENT.md 正文）
        allowed_spawns: list[str]，本 Agent 能 spawn 的子 agent（空 = 不能 spawn）
        tools: list[Tool]，从 tools.py 的 TOOLS 加载的工具实例
    """

    #: Agent 类型标识符，对应 agents/ 下的目录名（如 "paper-agent"）
    name: str

    #: 简短描述，供 LLM 在 Supervisor 选择 spawn 目标时参考
    description: str = ""

    #: 注入 LLM system prompt 的完整文本，定义 Agent 的行为规范
    system_prompt: str = ""

    #: Spawn 权限:本 Agent 能 spawn 哪些子 agent(spawn 工具运行时校验)
    allowed_spawns: list[str] = field(default_factory=list)

    #: 本 Agent 拥有的 Tool 实例列表，从 tools.py 的 TOOLS 列表加载
    tools: list[Tool] = field(default_factory=list)

