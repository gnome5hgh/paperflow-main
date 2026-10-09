"""qa-agent 的工具装配：阅读 + RAG 检索 + 笔记/记忆查询 + 图表问答。

装配 read_pdf/read_file(阅读论文与笔记)、glob/grep(定位文件)、ask_user_question
交互工具(回答模式/深度歧义时中途问用户)、analyze_figures(图表问答 mode,定位论文后
按 figure=N 单图分析作答)。已读记录随对话落盘自动完成,不单独装配 mark_read 工具。
不装配任何记忆工具——记忆读写已收归 memory-agent,本角色要记账只能派发它。
不装配语料检索——`rag_retrieve` 已收归 rag-agent(要检索就派发它)。
不装配任何"格式化最终回答"类的工具——回答的内容安全由安全中间件的 on_finish
钩子统一兜底。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, GlobTool, GrepTool, AskUserQuestionTool,
)
from paperflow.tools.vision.analyze_figures import AnalyzeFiguresTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ReadFileTool, GlobTool, GrepTool, AskUserQuestionTool,
    AnalyzeFiguresTool(),
])
