"""Skill 服务：注册表、准入安装与装配并入。

`SkillConfig`（插件配置实体）在 `../domain/`。
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
from paperflow.core.skills.services.registry import SkillRegistry

__all__ = [
    "SkillRegistry",
    "merge_tools",
    "install_skill",
    "update_skill",
    "uninstall_skill",
    "enable_skill",
    "list_skills_command",
    "load_lock",
]
