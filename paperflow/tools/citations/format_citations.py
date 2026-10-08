"""format_citations：把引用 key 渲染成参考文献段（文档末尾用）。"""
from paperflow.core.tool import Tool, ToolResult


class FormatCitationsTool(Tool):
    """把引用 key 列表渲染成参考文献段的工具。

    Attributes:
        name: str，工具名 "format_citations"
        description: str，工具描述
        parameters: dict，JSON Schema（keys/style）
        risk_level: str，"low"
        manager: CitationManager，注入的引用库门面
    """
    name = "format_citations"
    description = "把引用 key 列表渲染成参考文献段，追加到文档末尾。"
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
        """注入引用库门面。

        Args:
            manager: CitationManager，引用库读写入口
        """
        self.manager = manager

    def execute(self, keys: list[str], style: str = "author-year") -> ToolResult:
        """按样式渲染给定的引用 key 列表。

        Args:
            keys: list[str]，引用 key（未知 key 静默跳过）
            style: str，渲染样式：author-year/numbered/bibtex/gbt7714

        Returns:
            ToolResult，文本为渲染后的参考文献段。
        """
        return ToolResult(text=self.manager.format(keys, style))
