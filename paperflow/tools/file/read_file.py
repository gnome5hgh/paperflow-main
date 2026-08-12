"""ReadFileTool：读取资料库内各类根目录下的文本文件。

安全边界由中间件强制:path 参数经工作区白名单校验,读取外部内容会打"未经安全校验"
横幅。工具自身不重复校验——声明元数据即可。
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
    allowed_roots = ["note", "pdf", "memory", "templates", "scratch", "outline"]
    output_scan = "mark"                       # 外部文件内容 → SecurityScan 打未校验横幅
    side_effects = ["read_file"]

    def execute(self, path: str) -> ToolResult:
        """读取 path 指向的文本文件并原样返回(编码固定为 UTF-8)。"""
        return ToolResult(text=Path(path).read_text(encoding="utf-8"))
