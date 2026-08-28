"""lookup_citation：引用前必查——确认论文是否在语料库并拿到 key。"""
from paperflow.core.tool import Tool, ToolResult


class LookupCitationTool(Tool):
    name = "lookup_citation"
    description = ("查一篇论文是否在语料库（干净全标题或文件路径），返回引用 key 与状态。"
                   "引用前必查：status=in_corpus → 用 [来源:key§节]；status=missing → "
                   "标 [⚠无支撑]，不得编造引用。")
    parameters = {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "论文干净全标题（来自笔记 H1 / read_pdf 标题 / 用户）"},
            "path": {"type": "string", "format": "path", "description": "语料内文件绝对路径（可选，兜底入口）"},
        },
        "required": [],
    }
    risk_level = "low"
    allowed_roots = ["note", "pdf"]   # path 可能是笔记或 PDF 源路径

    def __init__(self, manager):
        """注入 CitationManager（引用库路径来自 config，非 LLM 可控）。"""
        self.manager = manager

    def execute(self, title: str | None = None, path: str | None = None) -> ToolResult:
        r = self.manager.resolve(path or title or "")
        text = (f"status: {r.status}\n"
                f"key: {r.key or '（无）'}\n"
                f"title: {r.title or '（未命中）'}\n"
                f"year: {r.year or '（未知）'}\n"
                f"note: {r.note_path or '无'}\n"
                f"pdf: {r.pdf_path or '无'}\n"
                f"（in_corpus → [来源:key§节]；missing → [⚠无支撑]，不编造）")
        return ToolResult(text=text, summary={"status": r.status, "key": r.key})
