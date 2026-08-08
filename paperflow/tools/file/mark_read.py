"""MarkReadTool：标记已读——只把路径记入 history.jsonl，不读文件内容。

pdf 根声明与只读边界一致；写入经 MemoryStore.append_history（已带 self._lock）。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult


class MarkReadTool(Tool):
    """标记已读：只把 pdf 路径记入 history.jsonl，不读文件内容。

    pdf 根声明与只读边界一致；写入经 MemoryStore.append_history（已带 self._lock）。
    """

    name = "mark_read"
    description = "标记某篇论文/笔记为已读（记录到阅读历史）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "论文/笔记绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    allowed_roots = ["pdf"]                    # MINOR-6：对齐 spec §14，mark-read 针对论文（pdf 只读根）
    side_effects = ["write_file"]

    def execute(self, path: str) -> ToolResult:
        from paperflow.core.memory.experience_memory import MemoryStore
        from paperflow.config import PaperFlowConfig
        config = PaperFlowConfig.from_env()
        store = MemoryStore(Path(config.workspace) / "memory")
        store.append_history({"type": "mark_read", "path": path})
        return ToolResult(text=f"已标记已读: {path}")
