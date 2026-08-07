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
        # 模板与 scratch 统一 workspace 派生基准（修 Layer 2 分叉根因）：
        # FormatCheckTool 默认同此基准，_SKELETON 仅降级
        "templates": str(Path(config.workspace) / "templates"),
        "scratch": str(Path(config.workspace) / "tmp"),
    }


def make_tools(config: PaperFlowConfig, tool_items: list) -> list[Tool]:
    """装配工具列表，兼容"类"与"已实例化工具"两种传参。

    类（ReadFileTool 等无参原子工具）经 cls() 实例化；已实例化工具（如
    SpawnSubAgentTool(agent_timeouts=...)——需要构造参数，无参 cls() 会
    TypeError）直接复用同一实例（Task 7：generate-note 审稿装配走此分支）。
    isinstance(item, type) 判定类是"可实例化"，否则视为现成实例。"""
    roots = _root_map(config)
    tools = []
    for item in tool_items:
        tool = item() if isinstance(item, type) else item
        # 赋新列表而非 in-place 变异（allowed_paths 是共享类属性，变异会污染所有子类）
        tool.allowed_paths = [roots[r] for r in tool.allowed_roots if r in roots]
        # 注入 config：依赖配置派生路径的工具（如 ReviewDraftTool scratch）用它，
        # 避免经 get_rag_service().config 间接取（agent 专属模块的命名空间无法 monkeypatch）
        tool._config = config
        # 路径发现：把解析后的绝对路径追加进 description，LLM 经 function schema 可见。
        # 排除 scratch——scratch 路径对 LLM 不透明（draft 路径由任务文本给出，无 ls 工具枚举不到）
        for r in tool.allowed_roots:
            if r in roots and r != "scratch":
                tool.description += f"\n[目录] {r}={roots[r]}"
        tools.append(tool)
    return tools
