# paperflow/terminal/activity.py
"""工具事件 → ZCode 风格活动行的映射与格式化（纯函数，无 I/O）。

映射表是「工具名 → 图标+动词+计数词」的唯一领域知识所在处；渲染器
（render.py 活动流模式）只管 live 区调度、聚合状态机与落屏时机。
聚合规则见 spec §4.1：连续同动词合并计数、聚合行省略摘要、
耗时 ≥ SLOW_MS 才标注、子 agent 调用加 [agent] 前缀。
"""

#: 慢操作阈值：tool_end 耗时 ≥ 此值（毫秒）才在活动行标注耗时
SLOW_MS = 2000

#: 工具名 → (emoji+动词, 计数词)。未知工具走 _DEFAULT。
ACTIVITY_LABELS: dict[str, tuple[str, str]] = {
    "read_file": ("🔍 查阅", "文件"),
    "read_pdf": ("🔍 查阅", "文件"),
    "grep": ("🔍 检索", "次"),
    "glob": ("🔍 检索", "次"),
    "rag_retrieve": ("📚 检索", "次"),
    "write_file": ("✏️ 写入", "文件"),
    "edit_file": ("✏️ 编辑", "文件"),
    "spawn_sub_agent": ("🤖 子任务", "次"),
}
_DEFAULT = ("🔧 执行", "次")


def activity_label(tool_name: str) -> tuple[str, str]:
    """工具名 → (emoji+动词, 计数词)；MCP 工具解析 mcp__<srv>__<tool> 前缀。"""
    if tool_name and tool_name.startswith("mcp__"):
        parts = tool_name.split("__")
        server = parts[1] if len(parts) > 1 and parts[1] else "?"
        tool = parts[2] if len(parts) > 2 and parts[2] else "?"
        return (f"🔌 {server} · {tool}", "次")
    return ACTIVITY_LABELS.get(tool_name or "", _DEFAULT)


def format_activity(verb: str, agent_type: str, root: str, *,
                    count: int = 1, count_word: str = "次",
                    summary: str | None = None,
                    duration_ms: int | None = None,
                    done: bool = False) -> str:
    """活动行文本。

    - count>1：聚合行，显示 `N <count_word>`，省略摘要（spec §4.1）
    - done：终态落屏，追加 ` ✓`；耗时 ≥ SLOW_MS 追加 ` · X.Xs`
    - agent_type != root：行首加 `[<agent>] ` 前缀（沿用现状前缀约定）
    """
    parts = [verb]
    if count > 1:
        parts.append(f"{count} {count_word}")
    elif summary:
        parts.append(summary)
    if done and duration_ms is not None and duration_ms >= SLOW_MS:
        parts.append(f"{duration_ms / 1000:.1f}s")
    line = " · ".join(parts)
    if agent_type != root:
        line = f"[{agent_type}] {line}"
    if done:
        line += " ✓"
    return line
