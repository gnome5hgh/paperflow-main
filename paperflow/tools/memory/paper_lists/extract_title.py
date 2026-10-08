"""ExtractTitleTool：提取论文权威标题（PDF 提取链或用户直接提供，禁文件名）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.memory.runtime_context import get_memory_context


class ExtractTitleTool(Tool):
    """提取论文权威标题的工具（PDF 提取链或用户直供，禁用文件名）。

    Attributes:
        name: str，工具名 "extract_title"
        description: str，工具描述
        parameters: dict，JSON Schema（pdf_path/title）
        risk_level: str，"medium"
    """
    name = "extract_title"
    description = "提取论文权威标题（PDF 提取链或用户直接提供，禁文件名）"
    parameters = {
        "type": "object",
        "properties": {
            "pdf_path": {"type": "string"},
            "title": {"type": "string"},
        },
        "required": [],
    }
    risk_level = "medium"    # 变异/信号类工具一律 medium；只有检索类工具为 low

    def execute(self, pdf_path: str | None = None, title: str | None = None) -> ToolResult:
        """返回权威标题；用户直供 title 直接采用（禁文件名的守门在调用方）。

        Args:
            pdf_path: str | None，PDF 路径（走提取链）
            title: str | None，用户直供标题（直接采用）

        Returns:
            ToolResult，文本形如 "title: …\nsource: …"；提取失败返回错误提示。
        """
        if title:    # 用户已给标题 → 直接用（禁文件名的守门在调用方）
            return ToolResult(text=f"title: {title}\nsource: search")
        ctx = get_memory_context()
        if ctx is None:
            return ToolResult(text="记忆服务未装配，记忆工具不可用")
        try:
            ex = ctx.title_extractor
            if ex is None:
                return ToolResult(text="Error: title extractor not available")
            r = ex.extract(pdf_path=pdf_path)
            if r.title:
                return ToolResult(text=f"title: {r.title}\nsource: {r.source}")
            return ToolResult(text="Error: 标题提取失败，请提供论文标题")
        except Exception as e:
            return ToolResult(text=f"Error: {e}")
