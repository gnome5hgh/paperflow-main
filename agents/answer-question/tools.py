"""answer-question 的工具装配：阅读 + RAG + 笔记查询 + 格式化。"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools import ReadFileTool, ReadPdfTool, MarkReadTool, FormatAnswerTool
from paperflow.rag.retriever import RagRetrieveTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    RagRetrieveTool, ReadPdfTool, ReadFileTool, MarkReadTool, FormatAnswerTool,
])
