"""review-note 的工具装配：审稿专用 4 工具。

被 generate-note 的 ReviewDraftTool 嵌套调用（子 agent，单目标）；无 spawn。
草稿/原文路径由任务文本给出（draft_path/pdf_path），非工具参数。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools import ReadFileTool, ReadPdfTool, FormatCheckTool, SuggestEditTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadFileTool, ReadPdfTool, FormatCheckTool, SuggestEditTool,
])
