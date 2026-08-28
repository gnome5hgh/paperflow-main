"""format_citations：把引用 key 渲染成参考文献段（大纲末尾用）。"""
from paperflow.core.tool import Tool, ToolResult


class FormatCitationsTool(Tool):
    name = "format_citations"
    description = "把引用 key 列表渲染成参考文献段，追加到大纲/文档末尾。"
    parameters = {
        "type": "object",
        "properties": {
            "keys": {"type": "array", "items": {"type": "string"},
                     "description": "引用 key 列表（来自 lookup_citation）"},
            "style": {"type": "string", "enum": ["author-year", "numbered", "bibtex", "gbt7714"],
                      "default": "author-year"},
        },
        "required": ["keys"],
    }
    risk_level = "low"

    def __init__(self, manager):
        self.manager = manager

    def execute(self, keys: list[str], style: str = "author-year") -> ToolResult:
        return ToolResult(text=self.manager.format(keys, style))
