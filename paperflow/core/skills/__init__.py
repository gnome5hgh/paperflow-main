# paperflow/core/skills/__init__.py
"""Skill 域包 —— 可安装能力包的注册表、装配并入与准入安装。

公共出口（下游一律 `from paperflow.core.skills import ...`）：
- ``SkillRegistry`` / ``SkillConfig`` ← registry（运行时发现/校验/可见性/渐进披露）
- ``install_skill`` / ``update_skill`` / ``uninstall_skill`` / ``enable_skill``
  / ``list_skills_command`` / ``load_lock`` ← install（准入/更新/停用与 lock 治理）
- ``merge_tools`` ← assembly（装配期工具并入 + 命名空间唯一性）
依赖方向：本包依赖 core.tool / core.common.frontmatter；core.agent 经本包做 L1 注入与工具并入。
"""

from paperflow.core.skills.services.assembly import merge_tools
from paperflow.core.skills.services.install import (
    enable_skill,
    install_skill,
    list_skills_command,
    load_lock,
    uninstall_skill,
    update_skill,
)
from paperflow.core.skills.domain import SkillConfig
from paperflow.core.skills.services.registry import SkillRegistry

__all__ = [
    "SkillConfig",
    "SkillRegistry",
    "enable_skill",
    "install_skill",
    "list_skills_command",
    "load_lock",
    "merge_tools",
    "uninstall_skill",
    "update_skill",
]
