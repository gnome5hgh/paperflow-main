# paperflow/core/skills/assembly.py
"""装配期 skill 接线 —— 工具并入与命名空间唯一性校验。

skill 工具在此并入 AgentConfig.tools 后，对框架就是普通 Tool：
PolicyEngine / Audit / WorkspacePolicy / 确认看门狗原样生效，安全管道零改动。
"""

from paperflow.core.constants import RISK_ORDER
from paperflow.core.tool import Tool

#: 外部来源工具的风险下限：第三方代码自报的档位不可信——安装时的人工过目只覆盖
#: 当时那一版，而 skill 可以更新换版本。故装配时抬到至少这一档（逐次确认）。
EXTERNAL_RISK_FLOOR = "high"


def _raise_to(tool: Tool, floor: str) -> None:
    """把工具风险档抬到至少 floor（已不低于则不动；未知档位按最高处理，不动它）。"""
    current = RISK_ORDER.get(tool.risk_level, RISK_ORDER["critical"])
    if current < RISK_ORDER[floor]:
        tool.risk_level = floor


def apply_pre_approval(tools: list[Tool], approved: set[str],
                       level: str = "medium") -> None:
    """预批准名单内的工具降到 level（**降档表达信任，不是旁路**）。

    降档而非旁路：工具仍走策略引擎的同一条判据，会话把免问上限调严时它照样会被问。
    已经是更低档的工具不抬高。

    Args:
        tools: list[Tool]，待处理的工具
        approved: set[str]，预批准的工具名（来自 security.pre_approved_tools 与
            mcp_servers.*.write_tools 的并集）
        level: str，降到哪一档
    """
    for tool in tools:
        if tool.name in approved:
            current = RISK_ORDER.get(tool.risk_level, RISK_ORDER["critical"])
            if current > RISK_ORDER[level]:
                tool.risk_level = level


def merge_tools(*groups: tuple[str, list[Tool]]) -> list[Tool]:
    """按组顺序合并工具列表，工具名全局唯一，冲突即抛 ValueError（fail-fast）。

    来源组为 ``"skill"`` 的工具在合并时被抬到至少 ``EXTERNAL_RISK_FLOOR``——它们是
    第三方代码，自报档位不可信（与 MCP 侧「注解不可信、缺注解按可能写」同构）。

    Args:
        groups: (来源标签, 工具列表) 序列，如 ("agent", ...), ("skill", ...), ("framework", ...)

    Raises:
        ValueError: 任一工具名重复（skill 夹带 load_skill 等框架名在此被拦）

    """
    merged: list[Tool] = []
    seen: dict[str, str] = {}
    for label, tools in groups:
        for tool in tools:
            if tool.name in seen:
                raise ValueError(
                    f"工具名冲突: '{tool.name}' 同时来自 {seen[tool.name]} 与 {label}"
                )
            seen[tool.name] = label
            if label == "skill":
                _raise_to(tool, EXTERNAL_RISK_FLOOR)
            merged.append(tool)
    return merged
