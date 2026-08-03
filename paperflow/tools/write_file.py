"""WriteFileTool：新建笔记（存在性守卫——新建专用，覆盖修改请用 edit_file）。

写后调 index_document() 热更新钩子，新内容会话内立即可检索（Layer 2 决策）。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service
from paperflow.tools._constants import NOTE_ROOTS


class WriteFileTool(Tool):
    name = "write_file"
    description = "写入新笔记（Note，非 Paper）到 note/ 或 memory/ 目录；已存在的文件将被拒绝（新建专用，修改请用 edit_file）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "目标文件绝对路径（不得已存在）"},
            "content": {"type": "string", "format": "content", "description": "文件内容"},
        },
        "required": ["path", "content"],
    }
    risk_level = "medium"                      # 写操作；新建可丢弃重来 → medium
    requires_confirm = True
    allowed_roots = NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        # 存在性守卫：Write 只用于新建（medium 理由"新建可丢弃重来"只在真新建时成立）。
        # 覆盖既有文件必须走 edit_file（high，需会话确认）——否则 LLM 可用 write_file
        # 绕过 edit 的 high 风险闸门。这是风险语义（强制工具选择），非安全边界——
        # 真正边界是 WorkspacePolicy 的 allowed_roots。不做锁、不做二次检查（TOCTOU 可接受）。
        if p.exists():
            return ToolResult(text=f"文件已存在，请用 edit_file 修改（write_file 仅用于新建）: {path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        get_rag_service().index_document(str(p))   # 热更新钩子：会话内立即可检索
        return ToolResult(text=f"已写入 {path}")
