# paperflow/core/assembly.py
"""装配期 skill 接线 —— 工具并入与命名空间唯一性校验。

skill 工具在此并入 AgentConfig.tools 后，对框架就是普通 Tool：
PolicyEngine / Audit / WorkspacePolicy / 确认看门狗原样生效，安全管道零改动。
"""

from paperflow.core.tool import Tool


def merge_tools(*groups: tuple[str, list[Tool]]) -> list[Tool]:
    """按组顺序合并工具列表，工具名全局唯一，冲突即抛 ValueError（fail-fast）。

    :param groups: (来源标签, 工具列表) 序列，如 ("agent", ...), ("skill", ...), ("framework", ...)
    :raises ValueError: 任一工具名重复（skill 夹带 load_skill 等框架名在此被拦）
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
            merged.append(tool)
    return merged
