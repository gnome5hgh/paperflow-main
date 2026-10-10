"""Skill 插件配置：`SkillRegistry` 扫描 `.paperflow/skills/<name>/` 的产物。"""
from dataclasses import dataclass, field
from pathlib import Path

from paperflow.core.tool import Tool


@dataclass
class SkillConfig:
    """单个 Skill 的完整配置，由 SkillRegistry 从 skills/<name>/ 目录加载。

    字段来源::

        name            ← SKILL.md frontmatter "name"（必须等于目录名，agentskills.io 规范）
        description     ← SKILL.md frontmatter "description"（做什么 + 何时触发，L1 消费）
        instructions    ← SKILL.md 正文（frontmatter 后的 Markdown，L2 按需加载）
        metadata        ← SKILL.md frontmatter "metadata"（version/author 等，lock 与清单展示用）
        tools           ← tools.py 模块级 TOOLS 列表（可选，装配期并入 agent 工具表）
        path            ← skill 目录路径（load_resource 读取资源的围栏根）

    Attributes:
        name: str，skill 名（须等于目录名）
        description: str，做什么 + 何时触发（L1 清单消费）
        instructions: str，SKILL.md 正文（L2 按需加载）
        metadata: dict，frontmatter metadata（version/author 等）
        tools: list[Tool]，tools.py 的 TOOLS（装配期并入 agent 工具表）
        path: Path | None，skill 目录（L3 资源读取的围栏根）
    """

    name: str
    description: str = ""
    instructions: str = ""
    metadata: dict = field(default_factory=dict)
    tools: list[Tool] = field(default_factory=list)
    path: Path | None = None

    @property
    def has_code(self) -> bool:
        """是否捆绑可执行 Tool 代码（安装准入时决定是否强制过目）。"""
        return bool(self.tools)

