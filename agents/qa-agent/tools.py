"""qa-agent 的工具装配：阅读 + RAG 检索 + 笔记/记忆查询。

装配 read_pdf/read_file(阅读论文与笔记)、RagRetrieveTool(从知识库检索相关段落
回答开放问题)、glob/grep(定位文件)。已读不再用 mark_read 单独标记——阅读随
对话落盘自动记录（Letta 记忆栈接管）。不装配任何"格式化最终回答"类的工具——
回答的内容安全由安全中间件的 on_finish 钩子统一兜底。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools import ReadFileTool, ReadPdfTool, GlobTool, GrepTool
from paperflow.rag.retriever import RagRetrieveTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    RagRetrieveTool, ReadPdfTool, ReadFileTool, GlobTool, GrepTool,
])
