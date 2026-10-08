"""add_citation：把论文加入 references.bib（语料库内 PDF，或库外 EXTERNAL）。"""
from paperflow.core.tool import Tool, ToolResult


class AddCitationTool(Tool):
    """把论文加入 references.bib 的工具（语料库内 PDF 或库外 EXTERNAL）。

    Attributes:
        name: str，工具名 "add_citation"
        description: str，工具描述
        parameters: dict，JSON Schema（pdf_path/title/authors/year/journal/external）
        risk_level: str，"medium"（写文件）
        side_effects: list[str]，["write_file"]
        root_hints: list[str]，["pdf"]（pdf_path 属语料库 pdf 根）
        manager: CitationManager，注入的引用库门面
    """
    name = "add_citation"
    description = ("把论文加入 references.bib（引用库）。语料库内论文传 pdf_path；"
                   "库外真实文献（用户确认存在）传 title+authors+year 且 external=true。")
    parameters = {
        "type": "object",
        "properties": {
            "pdf_path": {"type": "string", "format": "path", "description": "语料库内 PDF 绝对路径"},
            "title": {"type": "string", "description": "库外文献标题（external=true 时用）"},
            "authors": {"type": "string", "description": "作者，如 'Doe, John'（external 时）"},
            "year": {"type": "string"},
            "journal": {"type": "string"},
            "external": {"type": "boolean", "description": "库外真实文献（非语料库内）"},
        },
        "required": [],
    }
    risk_level = "medium"              # 写文件
    side_effects = ["write_file"]
    root_hints = ["pdf"]            # pdf_path 是语料库内 PDF（语料库 pdf 根）

    def __init__(self, manager):
        """注入引用库门面。

        Args:
            manager: CitationManager，引用库读写入口
        """
        self.manager = manager

    def execute(self, pdf_path: str | None = None, title: str | None = None,
                authors: str = "", year: str = "", journal: str = "",
                external: bool = False) -> ToolResult:
        """按 external 分流：库外文献用 title+authors+year 入库，语料库内 PDF 走 add_from_pdf。

        Args:
            pdf_path: str | None，语料库内 PDF 绝对路径
            title: str | None，库外文献标题
            authors: str，作者（external 时）
            year: str，年份
            journal: str，期刊
            external: bool，是否库外真实文献

        Returns:
            ToolResult，文本含 key/created/note/bib 路径；参数不足返回错误提示。
        """
        if external:
            r = self.manager.add_external(title or "", authors, year, journal)
        elif pdf_path:
            r = self.manager.add_from_pdf(pdf_path)
        else:
            return ToolResult(text="Error: 需要 pdf_path（语料库内）或 external=true + title（库外）")
        text = (f"key: {r['key'] or '（失败）'}\ncreated: {r['created']}\n"
                f"note: {r.get('note', '')}\nbib: {self.manager.bib_path}")
        return ToolResult(text=text, summary={"key": r.get("key"), "created": r.get("created")})
