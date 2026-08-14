"""qa-agent 的工具装配：阅读 + RAG 检索 + 笔记/记忆查询。

装配 read_pdf/read_file(阅读论文与笔记)、RagRetrieveTool(从知识库检索相关段落
回答开放问题)、glob/grep(定位文件)、ask_user_question 交互工具(回答模式/深度
歧义时中途问用户)。已读记录随对话落盘自动完成,不单独装配 mark_read 工具。
不装配任何"格式化最终回答"类的工具——回答的内容安全由安全中间件的 on_finish
钩子统一兜底。
"""
from paperflow.config import PaperFlowConfig
from paperflow.core.memory.tools import (
    ConversationSearchTool, ArchivalMemorySearchTool, ArchivalMemoryInsertTool,
    ExtractTitleTool, UnreadListAddTool, UnreadListRemoveTool, HistoryAppendTool,
)
from paperflow.tools.common.factory import make_tools
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, GlobTool, GrepTool, AskUserQuestionTool,
)
from paperflow.rag.services.retriever import RagRetrieveTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    RagRetrieveTool, ReadPdfTool, ReadFileTool, GlobTool, GrepTool, AskUserQuestionTool,
    ConversationSearchTool, ArchivalMemorySearchTool, ArchivalMemoryInsertTool,
    ExtractTitleTool, UnreadListAddTool, UnreadListRemoveTool, HistoryAppendTool,
])
