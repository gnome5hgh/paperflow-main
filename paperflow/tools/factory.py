"""make_tools：allowed_roots 语义根 → 配置解析 → 绝对路径注入 allowed_paths。

allowed_paths 保持 Layer 1 "绝对路径列表"语义，WorkspacePolicy 零改动；
根解析集中一处，测试可直接构造带 tmp_path 的 config 调用。
Layer 3 agent 的 tools.py 写：TOOLS = make_tools(PaperFlowConfig.from_env(), [ReadFileTool, ...])
（config 在 import 时构造——每进程静态、无副作用，勿改成运行时注入绕一圈。）
"""
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.tool import Tool


def _root_map(config: PaperFlowConfig) -> dict[str, str]:
    """语义根名 → 绝对路径（vault 为外部绝对路径，memory 随 workspace）。"""
    return {
        "note": config.vault_note_dir,
        "pdf": config.vault_pdf_dir,
        "memory": str(Path(config.workspace) / "memory"),
    }


def make_tools(config: PaperFlowConfig, tool_classes: list[type[Tool]]) -> list[Tool]:
    roots = _root_map(config)
    tools = []
    for cls in tool_classes:
        tool = cls()
        # 赋新列表而非 in-place 变异（allowed_paths 是共享类属性，变异会污染所有子类）
        tool.allowed_paths = [roots[r] for r in tool.allowed_roots if r in roots]
        tools.append(tool)
    return tools
