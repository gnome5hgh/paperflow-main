"""文件类原子 Tool：Read/Write/Edit/ReadPdf/MarkRead/FormatAnswer/FormatCheck/SuggestEdit。

安全边界靠中间件强制：format="path" → WorkspacePolicy（绝对路径 + allowed_roots 白名单）、
format="content" → SecurityScan（critical 硬阻断）、output_scan="mark" → 外部内容横幅。
Tool 自身不重复校验——声明元数据即可。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service

#: 可写的目录语义根（Note 可写；Paper=pdf 只读，SCOPE 硬边界）
_NOTE_ROOTS = ["note", "memory"]


class ReadFileTool(Tool):
    name = "read_file"
    description = "读取笔记/论文 PDF 路径/记忆目录下的文本文件内容"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "文件绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    allowed_roots = ["note", "pdf", "memory"]
    output_scan = "mark"                       # 外部文件内容 → SecurityScan 打未校验横幅
    side_effects = ["read_file"]

    def execute(self, path: str) -> ToolResult:
        return ToolResult(text=Path(path).read_text(encoding="utf-8"))


class WriteFileTool(Tool):
    name = "write_file"
    description = "写入新笔记（Note，非 Paper）到 note/ 或 memory/ 目录"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "目标文件绝对路径"},
            "content": {"type": "string", "format": "content", "description": "文件内容"},
        },
        "required": ["path", "content"],
    }
    risk_level = "medium"                      # 写操作；新建可丢弃重来 → medium
    requires_confirm = True
    allowed_roots = _NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        get_rag_service().index_document(str(p))   # 热更新钩子：会话内立即可检索
        return ToolResult(text=f"已写入 {path}")


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
    allowed_roots = _NOTE_ROOTS
    side_effects = ["write_file"]

    def execute(self, path: str, content: str) -> ToolResult:
        p = Path(path)
        if not p.exists():
            return ToolResult(text=f"文件不存在: {path}")
        p.write_text(content, encoding="utf-8")
        get_rag_service().index_document(str(p))
        return ToolResult(text=f"已编辑 {path}")
