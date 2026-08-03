"""ReadPdfTool：GROBID 解析 PDF 为结构化文本，不可用时回退 PyMuPDF。

经 RAGService.pdf_parser() 获取解析器（GROBID 探测结果缓存于服务层，当次会话不回退）。
"""
from pathlib import Path

from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.service import get_rag_service


class ReadPdfTool(Tool):
    name = "read_pdf"
    description = "解析 PDF 论文为结构化文本（GROBID，不可用时回退 PyMuPDF）"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "format": "path", "description": "PDF 绝对路径"},
        },
        "required": ["path"],
    }
    risk_level = "low"
    allowed_roots = ["pdf"]                    # Paper 只读
    output_scan = "mark"
    side_effects = ["read_file"]

    def execute(self, path: str) -> ToolResult:
        parser = get_rag_service().pdf_parser()
        doc = parser.parse_pdf(path)
        text = "\n\n".join(f"## {h}\n{t}" for h, t in doc.sections)
        return ToolResult(text=text or "（PDF 未能解析出文本）")
