"""list_citations：检索 references.bib（模糊搜索，找候选用）。"""
from paperflow.core.tool import Tool, ToolResult


class ListCitationsTool(Tool):
    """模糊检索 references.bib 条目的工具（找候选用）。

    Attributes:
        name: str，工具名 "list_citations"
        description: str，工具描述
        parameters: dict，JSON Schema（search）
        risk_level: str，"low"
        manager: CitationManager，注入的引用库门面
    """
    name = "list_citations"
    description = "检索 references.bib 中的条目（按标题/作者/key 模糊搜索）。只记得论文大概名字时先搜索拿候选，再 lookup_citation。"
    parameters = {
        "type": "object",
        "properties": {"search": {"type": "string", "description": "搜索词（留空 = 全部）"}},
        "required": [],
    }
    risk_level = "low"

    def __init__(self, manager):
        """注入引用库门面。

        Args:
            manager: CitationManager，引用库读写入口
        """
        self.manager = manager

    def execute(self, search: str | None = None) -> ToolResult:
        """按搜索词列出候选条目。

        Args:
            search: str | None，搜索词（留空 = 全部）

        Returns:
            ToolResult，文本为 "- key: title (year)" 列表；库为空返回固定提示。
        """
        items = self.manager.search(search or "")
        lines = "\n".join(f"- {e.key}: {e.title} ({e.fields.get('year','')})" for e in items)
        return ToolResult(text=lines or "（引用库为空）")
