"""make_tools：把工具的语义根名解析成配置里的绝对路径,注入 allowed_paths。

allowed_paths 保持"绝对路径列表"语义,工作区校验层零改动;根解析集中一处,测试可
直接构造带临时路径的 config 调用。agent 的 tools.py 写:TOOLS = make_tools(
PaperFlowConfig.from_env(), [ReadFileTool, ...])(config 在 import 时构造——
每进程静态、无副作用,勿改成运行时注入绕一圈)。
"""
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.tool import Tool


def _root_map(config: PaperFlowConfig) -> dict[str, str]:
    """语义根名 → 绝对路径(vault 为外部绝对路径,memory 随 workspace)。"""
    return {
        "note": config.vault_note_dir,
        "pdf": config.vault_pdf_dir,
        "outline": config.vault_outline_dir or str(Path(config.workspace) / "outline"),
        "memory": str(Path(config.workspace) / "memory"),
        # 模板与 scratch 统一从 workspace 派生基准(FormatCheckTool 默认同此基准,骨架仅降级)
        "templates": str(Path(config.workspace) / "templates"),
        "scratch": str(Path(config.workspace) / "tmp"),
    }


def make_tools(config: PaperFlowConfig, tool_items: list[type[Tool] | Tool]) -> list[Tool]:
    """装配工具列表,兼容"类"与"已实例化工具"两种传参。

    类(如 ReadFileTool 等无参原子工具)经 cls() 实例化;已实例化工具(如
    SpawnSubAgentTool(agent_timeouts=...)——需要构造参数,无参 cls() 会抛
    TypeError)直接复用同一实例。isinstance(item, type) 判定类为"可实例化",
    否则视为现成实例。"""
    roots = _root_map(config)
    tools = []
    for item in tool_items:
        # 类走无参实例化；现成实例直接复用——两分支共用后续注入逻辑
        tool = item() if isinstance(item, type) else item
        # 赋新列表而非 in-place 变异（allowed_paths 是共享类属性，变异会污染所有子类）
        tool.allowed_paths = [roots[r] for r in tool.allowed_roots if r in roots]
        # 注入 config:依赖配置派生路径的工具直接读它,避免经 RAG 服务的全局单例间接取
        tool._config = config
        # 路径发现:把解析后的绝对路径追加进 description,LLM 经函数 schema 可见。
        # 排除 scratch——scratch 路径对 LLM 不透明(草稿路径由任务文本给出,无枚举工具)
        for r in tool.allowed_roots:
            if r in roots and r != "scratch":
                tool.description += f"\n[目录] {r}={roots[r]}"
        tools.append(tool)
    return tools
