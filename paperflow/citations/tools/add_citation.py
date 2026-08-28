"""add_citation：把论文加入 references.bib（语料库内 PDF，或库外 EXTERNAL）。"""
from paperflow.core.tool import Tool, ToolResult


class AddCitationTool(Tool):
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

    def __init__(self, manager):
        self.manager = manager

    def execute(self, pdf_path: str | None = None, title: str | None = None,
                authors: str = "", year: str = "", journal: str = "",
                external: bool = False) -> ToolResult:
        if external:
            r = self.manager.add_external(title or "", authors, year, journal)
        elif pdf_path:
            r = self.manager.add_from_pdf(pdf_path)
        else:
            return ToolResult(text="Error: 需要 pdf_path（语料库内）或 external=true + title（库外）")
        text = (f"key: {r['key'] or '（失败）'}\ncreated: {r['created']}\n"
                f"note: {r.get('note', '')}\nbib: {self.manager.bib_path}")
        return ToolResult(text=text, summary={"key": r.get("key"), "created": r.get("created")})
