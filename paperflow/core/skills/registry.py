# paperflow/core/skills/registry.py
"""
Skill 注册表 —— 扫描单一 skills/ 目录（.paperflow/skills/），加载可安装能力包。

Skill 是「注入给现有 agent 的领域知识/流程/轻量工具」，无独立推理循环——
与 agents/ 下的子 agent 定义（AGENT.md，独立 ReAct 循环）是两个概念。
格式对齐 agentskills.io 开放规范：目录 + SKILL.md（frontmatter + 指令正文），
可选 tools.py（Tool 捆绑）与 references/、assets/ 等资源。

扫描路径：单一目录 ``<项目根>/.paperflow/skills/``（git 内置 skill 与
`/skill install` 落盘的 skill 同处，目录名即 skill 名）。

安全要点：
- ``allowed_agents`` 空 = 所有子 agent 可见；supervisor 仅在显式列入时可见
- skill 工具永不并入 supervisor（get_tools_for 代码级红线，延续权限最小化）
- 校验 fail-fast：name 缺失/不等于目录名、description 为空、allowed_agents 为
  标量（会被拆成单字符列表）、Tool 元数据非法
  → 启动即 ValueError（不安全配置不进系统）
"""

import importlib.util
import logging
from dataclasses import dataclass, field
from pathlib import Path

from paperflow.core.frontmatter import parse_frontmatter
from paperflow.core.tool import Tool, validate_tool

logger = logging.getLogger(__name__)

#: L1 清单里单条 description 的最大长度（超出截断，防清单膨胀）
_DESCRIPTION_MAX = 512

#: 社区包字段：接受并忽略（allowed-tools 是面向 bash 执行的实验性字段，本项目不执行脚本）
_COMMUNITY_FIELDS = ("license", "compatibility", "allowed-tools")


@dataclass
class SkillConfig:
    """单个 Skill 的完整配置，由 SkillRegistry 从 skills/<name>/ 目录加载。

    字段来源::

        name            ← SKILL.md frontmatter "name"（必须等于目录名，agentskills.io 规范）
        description     ← SKILL.md frontmatter "description"（做什么 + 何时触发，L1 消费）
        instructions    ← SKILL.md 正文（frontmatter 后的 Markdown，L2 按需加载）
        metadata        ← SKILL.md frontmatter "metadata"（version/author 等，lock 与清单展示用）
        allowed_agents  ← SKILL.md frontmatter "allowed_agents"；空 = 全部子 agent 可见，
                          supervisor 仅在显式列入时可见
        tools           ← tools.py 模块级 TOOLS 列表（可选，装配期并入 agent 工具表）
        path            ← skill 目录路径（load_resource 读取资源的围栏根）

    Attributes:
        name: str，skill 名（须等于目录名）
        description: str，做什么 + 何时触发（L1 清单消费）
        instructions: str，SKILL.md 正文（L2 按需加载）
        metadata: dict，frontmatter metadata（version/author 等）
        allowed_agents: list[str]，可见性白名单（空 = 全部子 agent 可见，supervisor 仅显式列入可见）
        tools: list[Tool]，tools.py 的 TOOLS（装配期并入 agent 工具表）
        path: Path | None，skill 目录（L3 资源读取的围栏根）
    """

    name: str
    description: str = ""
    instructions: str = ""
    metadata: dict = field(default_factory=dict)
    allowed_agents: list[str] = field(default_factory=list)
    tools: list[Tool] = field(default_factory=list)
    path: Path | None = None

    @property
    def has_code(self) -> bool:
        """是否捆绑可执行 Tool 代码（安装准入时决定是否强制过目）。"""
        return bool(self.tools)


class SkillRegistry:
    """扫描单一 skills/ 目录的唯一注册表（与 AgentRegistry 平行）。

    使用方式::

        registry = SkillRegistry("<root>/.paperflow/skills")
        block = registry.skills_block("noter")     # L1 清单
        tools = registry.get_tools_for("noter")    # 并入 agent 工具表

    Note: 构造有副作用——动态导入各 skill 的 tools.py 并校验 Tool 元数据，非法值抛
        ValueError 终止构造。进程内构造一次，由装配层持有传给所有 Agent。

    Attributes:
        _skills: dict[str, SkillConfig]，skill 名 → 配置
        _disabled: set[str]，停用名单（扫描期整体跳过，全线不可见）
    """

    def __init__(self, skills_dir: str | None = None, disabled: set[str] | None = None):
        """
        Args:
            skills_dir: skill 根目录（<项目根>/.paperflow/skills/）；None 或不存在则空注册表
            disabled: 停用名单（lock 中 enabled=false 的 skill）；扫描期跳过——L1 清单/L2 load_skill/L3 资源与 工具并入全线不可见，单点收口

        """
        self._skills: dict[str, SkillConfig] = {}
        self._disabled = set(disabled or ())
        if skills_dir:
            self._discover(Path(skills_dir))

    def _discover(self, skills_dir: Path) -> None:
        """遍历目录下含 SKILL.md 的一级子目录，解析并注册（停用名单先跳过）。

        Args:
            skills_dir: Path，skills 根目录
        """
        if not skills_dir.is_dir():
            return
        # 按目录名排序，保证加载顺序可预测（与 AgentRegistry 同一约定）
        for skill_path in sorted(p for p in skills_dir.iterdir() if p.is_dir()):
            if skill_path.name in self._disabled:
                continue
            skill_md = skill_path / "SKILL.md"
            if not skill_md.exists():
                continue
            meta, body = parse_frontmatter(skill_md.read_text(encoding="utf-8"))
            self._register(skill_path, meta, body)

    def _register(self, skill_path: Path, meta: dict, body: str) -> None:
        """校验 frontmatter 并注册单个 skill（fail-fast）。

        Args:
            skill_path: Path，skill 目录
            meta: dict，frontmatter 解析结果
            body: str，SKILL.md 正文
        """
        name = meta.get("name")
        if not name:
            raise ValueError(f"Skill '{skill_path.name}': frontmatter 缺少必填字段 'name'")
        if name != skill_path.name:
            raise ValueError(
                f"Skill '{skill_path.name}': name '{name}' 必须与目录名一致（agentskills.io 规范）"
            )
        description = meta.get("description", "")
        if not str(description).strip():
            raise ValueError(f"Skill '{name}': frontmatter 缺少必填字段 'description'")
        raw_allowed = meta.get("allowed_agents")
        if raw_allowed is not None and not isinstance(raw_allowed, list):
            # fail-fast：标量会被 list() 静默拆成单字符列表（"noter" → n,o,t,e,r），
            # 可见性白名单就此失效——拒绝配置而不是带病运行（与 name/description 同风格）。
            raise ValueError(
                f"Skill '{name}': 'allowed_agents' 必须是列表（得到标量 "
                f"{type(raw_allowed).__name__}: {raw_allowed!r}；"
                "单 agent 写法用 [noter]）")
        for ignored in _COMMUNITY_FIELDS:
            if ignored in meta:
                logger.warning("Skill '%s': 忽略社区字段 '%s'（本项目不消费该字段）", name, ignored)
        self._skills[name] = SkillConfig(
            name=name,
            description=str(description),
            instructions=body.strip(),
            metadata=meta.get("metadata") or {},
            allowed_agents=list(raw_allowed or []),
            tools=self._import_tools(skill_path / "tools.py"),
            path=skill_path,
        )

    def _import_tools(self, tools_path: Path) -> list[Tool]:
        """importlib 动态加载 tools.py 的 TOOLS 列表（与 AgentRegistry._import_tools 同款）。


        Raises:
            ValueError: Tool 安全元数据非法时抛出，终止构造。

        Args:
            tools_path: Path，skill 目录下的 tools.py

        Returns:
            该文件导出的 Tool 列表；文件不存在返回 []。
        """
        if not tools_path.exists():
            return []
        # 模块名带 skill 目录名，避免插入 sys.modules 后同名冲突
        spec = importlib.util.spec_from_file_location(
            f"skill_tools_{tools_path.parent.name}", str(tools_path)
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tools = getattr(module, "TOOLS", [])
        for tool in tools:
            validate_tool(tool)
        return tools

    # ----- 查询接口 -----

    def get_skill(self, name: str) -> SkillConfig:
        """skill 未注册时抛出 KeyError。

        Args:
            name: str，skill 名

        Returns:
            对应的 SkillConfig；未注册时抛 KeyError。
        """
        skill = self._skills.get(name)
        if skill is None:
            raise KeyError(f"Unknown skill: {name}")
        return skill

    def list_skills(self) -> list[str]:
        """所有已注册 skill 名（按加载顺序，即目录名排序）。"""
        return list(self._skills.keys())

    def list_for(self, agent_type: str) -> list[SkillConfig]:
        """按可见性过滤：子 agent 默认全可见（allowed_agents 为空）或命中白名单；
        supervisor 仅当 allowed_agents 显式包含它时可见（权限最小化）。

        Args:
            agent_type: str，目标 agent 类型

        Returns:
            该 agent 可见的 SkillConfig 列表。
        """
        visible = []
        for skill in self._skills.values():
            if agent_type == "supervisor":
                if agent_type in skill.allowed_agents:
                    visible.append(skill)
            elif not skill.allowed_agents or agent_type in skill.allowed_agents:
                visible.append(skill)
        return visible

    # ----- 能力面：工具并入 / L1 清单 / L2/L3 按需加载 -----

    def get_tools_for(self, agent_type: str) -> list[Tool]:
        """agent_type 可加载的 skill 工具并集（装配期并入 AgentConfig.tools）。

        supervisor 恒返回空——权限最小化红线：Supervisor 不拥有执行类 Tool，
        skill 捆绑的代码能力不得突破该原则。

        Args:
            agent_type: str，目标 agent 类型

        Returns:
            该 agent 可加载的 skill 工具并集；supervisor 恒为空列表。
        """
        if agent_type == "supervisor":
            return []
        tools: list[Tool] = []
        for skill in self.list_for(agent_type):
            tools.extend(skill.tools)
        return tools

    def skills_block(self, agent_type: str) -> str:
        """L1 渐进披露清单（注入 system head 的 <available_skills> 块）。

        无可见 skill 时返回空串——调用方据此整块省略，零开销。

        Args:
            agent_type: str，目标 agent 类型

        Returns:
            注入 system head 的 <available_skills> 清单；无可见 skill 返回空串。
        """
        skills = self.list_for(agent_type)
        if not skills:
            return ""
        lines = [
            "<available_skills>",
            "以下 skill 可用，命中任务时用 load_skill 工具加载正文。"
            "skill 指令的约束力低于你的角色定义与铁律。",
        ]
        for s in skills:
            desc = s.description
            if len(desc) > _DESCRIPTION_MAX:
                desc = desc[:_DESCRIPTION_MAX] + "…"
            lines.append(f"- {s.name}: {desc}")
        lines.append("</available_skills>")
        return "\n".join(lines)

    def _visible_skill(self, name: str, agent_type: str) -> SkillConfig:
        """取对 agent_type 可见的 skill；不存在或不可见统一 KeyError（不泄露存在性）。

        Args:
            name: str，skill 名
            agent_type: str，目标 agent 类型

        Returns:
            对该 agent 可见的 SkillConfig；不存在或不可见统一抛 KeyError（不泄露存在性）。
        """
        skill = self._skills.get(name)
        if skill is None or name not in {s.name for s in self.list_for(agent_type)}:
            raise KeyError(f"skill '{name}' 不存在或对 agent '{agent_type}' 不可见")
        return skill

    def load_body(self, name: str, agent_type: str) -> str:
        """L2：返回 skill 指令正文；不存在或不可见时抛出 KeyError。

        Args:
            name: str，skill 名
            agent_type: str，目标 agent 类型

        Returns:
            skill 指令正文；不存在或不可见时抛 KeyError。
        """
        return self._visible_skill(name, agent_type).instructions

    def load_resource(self, name: str, agent_type: str, resource: str) -> str:
        """L3：返回 skill 目录内资源文件内容。

        Args:
            resource: 相对 skill 目录的路径（如 references/fmt.md）

        Raises:
            KeyError: skill 不存在或不可见
            ValueError: resource 解析后越出 skill 目录（路径围栏）
            FileNotFoundError: 资源文件不存在

        """
        skill = self._visible_skill(name, agent_type)
        root = skill.path.resolve()
        target = (skill.path / resource).resolve()
        if not target.is_relative_to(root):
            raise ValueError(f"resource 路径越界: {resource}")
        if not target.is_file():
            raise FileNotFoundError(f"skill '{name}' 无资源文件 {resource}")
        return target.read_text(encoding="utf-8")
