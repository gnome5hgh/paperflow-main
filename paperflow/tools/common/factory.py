"""make_tools：装配工具——[目录] 提示注入 + 默认写根盖章 + config 注入。

提示与强制分离：root_hints 仅生成 description 里的 [目录] 行，帮助 LLM 定位
语料库目录；真正的强制边界是「绝对路径 + 敏感路径黑名单」（WorkspacePolicyMiddleware）。
default_write_root 按 agent 装配注入（note-agent→"note"、research-agent→"research"）：
write_file 的 filename+dir 便捷入口省略 dir 时落到该根，防止产物错位（代码层强制）。
"""
from pathlib import Path

from paperflow.config import PaperFlowConfig
from paperflow.core.tool import Tool
from paperflow.tools.file.write_file import WriteFileTool


def _root_map(config: PaperFlowConfig) -> dict[str, str]:
    """语义根名 → 绝对路径(语料库目录为外部绝对路径,memory 随 workspace)。

    Args:
        config: PaperFlowConfig，路径来源

    Returns:
        语义根名 → 绝对路径的映射（note/pdf/research/memory/scratch）。
    """
    return {
        "note": config.corpus.note_dir,
        "pdf": config.corpus.pdf_dir,
        "research": config.corpus.research_dir or str(Path(config.runtime.workspace) / "research"),
        "memory": str(Path(config.runtime.workspace) / "memory"),
        # 模板已随写侧 skill 分发(见 format_check 的 _DEFAULT_TEMPLATE);scratch 仍从 workspace 派生
        "scratch": str(Path(config.runtime.workspace) / "scratch"),
    }


def make_tools(config: PaperFlowConfig, tool_items: list[type[Tool] | Tool],
               default_write_root: str | None = None) -> list[Tool]:
    """装配工具列表,兼容"类"与"已实例化工具"两种传参。

    类(如 ReadFileTool 等无参原子工具)经 cls() 实例化;已实例化工具(如
    SpawnSubAgentTool(agent_timeouts=...)——需要构造参数,无参 cls() 会抛
    TypeError)直接复用同一实例。isinstance(item, type) 判定类为"可实例化",
    否则视为现成实例。default_write_root 语义根名（"note"/"research"/…）非 None
    时,盖章到 WriteFileTool 实例的 _default_write_root（filename 模式缺省落点）。

    Args:
        config: PaperFlowConfig，注入给工具的配置
        tool_items: list[type[Tool] | Tool]，工具类或已实例化工具
        default_write_root: str | None，装配注入的默认写根（语义根名）

    Returns:
        装配好的 Tool 实例列表（已注入 [目录] 提示、默认写根与 _config）。
    """
    roots = _root_map(config)
    tools = []
    for item in tool_items:
        # 类走无参实例化；现成实例直接复用——两分支共用后续注入逻辑
        tool = item() if isinstance(item, type) else item
        # 路径发现:把解析后的绝对路径追加进 description,LLM 经函数 schema 可见。
        # 排除 scratch——scratch 路径对 LLM 不透明(草稿路径由任务文本给出,无枚举工具)
        for r in tool.root_hints:
            if r in roots and r != "scratch":
                tool.description += f"\n[目录] {r}={roots[r]}"
        if (default_write_root and isinstance(tool, WriteFileTool)
                and default_write_root in roots):
            tool._default_write_root = roots[default_write_root]
        # 注入 config:依赖配置派生路径的工具直接读它,避免经 RAG 服务的全局单例间接取
        tool._config = config
        tools.append(tool)
    return tools
