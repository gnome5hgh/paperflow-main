"""ExtractTitleTool：读 PDF 取论文标题（本地版面解析，禁文件名）。"""
from paperflow.core.tool import Tool, ToolResult
from paperflow.rag.parsers.pdf_extract import pdf_title


class ExtractTitleTool(Tool):
    """取论文权威标题：传 PDF 路径即读，用户直供标题则直接采用。

    归属工具层（与 read_pdf / analyze_figures 同批装配），实现调 RAG 解析器的
    标题出口——那处判据与索引/RAG 用的是同一套「宁空勿错」，所以工具取到的标题
    与语料索引里的标题必然一致。绝不用文件名充当标题：文件名是存储产物（下载时
    可能是乱码或编号），拿它当标题会污染 unread_list / history_list，让后续按标题
    去重与移除全部失准。

    Attributes:
        name: str，工具名 "extract_title"
        description: str，工具描述
        parameters: dict，JSON Schema（pdf_path / title）
        risk_level: str，"medium"（变异/信号类工具一律 medium）
    """

    name = "extract_title"
    description = ("取论文权威标题：给 pdf_path 读该 PDF 的标题；用户已给标题时"
                   "传 title 直接采用。判据宁空勿错——拿不准时返回空并提示，"
                   "此时请向用户问标题，绝不要拿文件名充当标题。")
    parameters = {
        "type": "object",
        "properties": {
            "pdf_path": {"type": "string", "description": "PDF 文件的绝对路径"},
            "title": {"type": "string", "description": "用户已给出的标题（优先于读取 PDF）"},
        },
        "required": [],
    }
    risk_level = "medium"

    def execute(self, pdf_path: str | None = None, title: str | None = None) -> ToolResult:
        """返回权威标题。

        Args:
            pdf_path: PDF 路径（读标题）。
            title: 用户直供标题，给了就用它，不再读 PDF。

        Returns:
            ToolResult，文本为 `title: …`；读不出来时给出可行动的提示
            （让调用方向用户问标题，而不是退而用文件名）。
        """
        if title:
            return ToolResult(text=f"title: {title}")
        if not pdf_path:
            return ToolResult(text="未提供 pdf_path 也未给 title：请向用户问论文标题。")
        try:
            found = pdf_title(pdf_path)
        except FileNotFoundError:
            return ToolResult(text=f"文件不存在：{pdf_path}", is_error=True)
        except Exception as e:
            return ToolResult(text=f"读取 PDF 标题失败：{e}", is_error=True)
        if found:
            return ToolResult(text=f"title: {found}")
        return ToolResult(
            text="未能从该 PDF 取到可靠标题（元数据缺失或候选不过判据）。"
                 "请向用户问标题，不要用文件名充当标题。")
