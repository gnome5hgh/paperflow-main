"""WriteFileTool：写入/重写笔记（新建+覆盖；整篇写/重写，小范围改动用 edit_file）。

写后调 index_document() 热更新钩子，新内容会话内立即可检索（Layer 2 决策）。
2026-08-06：删除"仅新建"存在性守卫——旧语义让已存在文件必须走 edit_file，但 edit_file
曾因 high>medium 被拒 → 已存在文件无任何可写工具（死锁）。现 write=整篇写/重写、
edit=定向 search-replace，两者同 medium 同确认。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service
from paperflow.tools.file._constants import NOTE_ROOTS


class WriteFileTool(Tool):
    name = "write_file"
    description = "写入或整篇重写笔记（Note，非 Paper）到 note/ 或 memory/ 目录；已存在的文件将被覆盖（小范围修改请用 edit_file 定向替换）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "目标文件绝对路径（不存在则新建，已存在则整篇覆盖）"},
            "content": {"type": "string", "format": "content", "description": "文件完整新内容"},
        },
        "required": ["path", "content"],
    }
    risk_level = "medium"                      # 写操作；全文覆盖可确认重来 → medium
    requires_confirm = True
    allowed_roots = NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        # 2026-08-06：write_file 支持新建+覆盖（medium+确认）。旧语义"仅新建"让已存在
        # 文件必须走 edit_file，但 edit_file 曾因 high>medium 被拒 → 已存在文件无任何可写
        # 工具（死锁）。现两者同 medium 同确认：write=整篇写/重写，edit=定向 search-replace。
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        get_rag_service().index_document(str(p))   # 热更新钩子：会话内立即可检索
        return ToolResult(text=f"已写入 {path}")
