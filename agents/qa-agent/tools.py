"""qa-agent 的工具装配：阅读 + RAG + 笔记查询。

不再装配 format_answer：真实 CLI 冒烟发现该 agent 会用它把最终回答格式化，
反而劣化回答质量（传入的常是"已读取"这类状态文本而非答案，产出无用输出）。
最终回答的内容安全扫描由 SecurityScanMiddleware.on_finish 兜底，移除无安全缺口。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools import ReadFileTool, ReadPdfTool, MarkReadTool, GlobTool, GrepTool
from paperflow.rag.retriever import RagRetrieveTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    RagRetrieveTool, ReadPdfTool, ReadFileTool, MarkReadTool, GlobTool, GrepTool,
])
