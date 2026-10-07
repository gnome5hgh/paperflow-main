# paperflow/core/skills/__init__.py
"""Skill 域包 —— 可安装能力包的注册表、装配并入与准入安装（ADR 0011）。

公共出口（下游一律 `from paperflow.core.skills import ...`）：
- ``SkillRegistry`` / ``SkillConfig`` ← registry（运行时发现/校验/可见性/渐进披露）
- ``install_skill`` / ``uninstall_skill`` / ``list_skills_command`` / ``load_lock`` ← install
- ``merge_tools`` ← assembly（装配期工具并入 + 命名空间唯一性）
依赖方向：本包依赖 core.tool / core.frontmatter；core.agent 经本包做 L1 注入与工具并入。
"""

from paperflow.core.skills.assembly import merge_tools
from paperflow.core.skills.install import (
    install_skill,
    list_skills_command,
    load_lock,
    uninstall_skill,
)
from paperflow.core.skills.registry import SkillConfig, SkillRegistry

__all__ = [
    "SkillConfig",
    "SkillRegistry",
    "install_skill",
    "list_skills_command",
    "load_lock",
    "merge_tools",
    "uninstall_skill",
]
