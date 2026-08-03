"""EditFileTool：修改既有笔记（覆盖式编辑，破坏性强于新建 → high）。

写后调 index_document() 热更新钩子（与 WriteFileTool 同款，索引一致维护）。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service
from paperflow.tools._constants import NOTE_ROOTS


class EditFileTool(Tool):
    name = "edit_file"
    description = "修改既有笔记内容（覆盖式编辑，破坏性强于新建）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "文件绝对路径"},
            "content": {"type": "string", "format": "content", "description": "新的完整内容"},
        },
        "required": ["path", "content"],
    }
    risk_level = "high"                        # 编辑修改既有内容 + 影响已索引文档 → high
    requires_confirm = True                    # 会话级确认；不放宽 max_risk
    allowed_roots = NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        if not p.exists():
            return ToolResult(text=f"文件不存在: {path}")
        p.write_text(content, encoding="utf-8")
        get_rag_service().index_document(str(p))
        return ToolResult(text=f"已编辑 {path}")
