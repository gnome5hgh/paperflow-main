"""Tool 安全元数据的加载期校验。

AgentRegistry 与 SkillRegistry 共用：不安全配置不进系统。
"""
from typing import TYPE_CHECKING

from paperflow.core.constants import RISK_LEVELS, SIDE_EFFECTS

if TYPE_CHECKING:
    from paperflow.core.tool.base import Tool


def validate_tool(tool: "Tool") -> None:
    """校验 Tool 的安全元数据字段，非法值立即抛 ValueError（加载时前置防线）。

    AgentRegistry 与 SkillRegistry 共用：不安全配置不进系统。
    校验点：risk_level ∈ RISK_LEVELS；side_effects 每个值 ∈ SIDE_EFFECTS；
    output_scan ∈ (None, "mark")。

    Args:
        tool: 待校验的 Tool 实例

    Raises:
        ValueError: 任一字段非法，携带工具名与合法值列表

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
