"""ReadFileTool：读取 note/pdf/memory/templates/scratch 根下的文本文件。

安全边界靠中间件强制：format="path" → WorkspacePolicy（绝对路径 + allowed_roots 白名单）、
output_scan="mark" → 外部内容横幅。Tool 自身不重复校验——声明元数据即可。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult


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
    # 读面含 templates（LLM 读模板）+ scratch（子 agent 读落盘桥草稿）
    allowed_roots = ["note", "pdf", "memory", "templates", "scratch"]
    output_scan = "mark"                       # 外部文件内容 → SecurityScan 打未校验横幅
    side_effects = ["read_file"]

    def execute(self, path: str) -> ToolResult:
        return ToolResult(text=Path(path).read_text(encoding="utf-8"))
