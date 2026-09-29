"""WriteFileTool：写入或整篇重写笔记(新建+覆盖;小范围改动用 edit_file)。

写入后调用索引热更新钩子,新内容会话内立即可被检索。write 负责整篇写/重写,
edit 负责定向替换,两者同为 medium 风险并需用户确认。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.services.rag_service import get_rag_service
from paperflow.tools.file._constants import NOTE_HINTS
from paperflow.tools.file.atomic import atomic_write


class WriteFileTool(Tool):
    name = "write_file"
    description = "写入或整篇重写笔记（Note，非 Paper）到 note/、memory/、research/ 目录；已存在的文件将被覆盖（小范围修改请用 edit_file 定向替换）"
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
    root_hints = NOTE_HINTS
    side_effects = ["write_file"]

    def execute(self, path: str, content: str) -> ToolResult:
        """写入/覆盖笔记文件,并做索引热更新使其立即可检索。"""
        p = Path(path)
        atomic_write(p, content)
        note = ""
        try:
            get_rag_service().index_document(str(p))
        except Exception as e:
            # Milvus 是外部服务可能未启动：索引失败只降级为提示，不掩盖已成功的写入
            note = f"（索引失败：{e}）"
        return ToolResult(text=f"已写入 {path}{note}", completion=f"File written: {path}")
