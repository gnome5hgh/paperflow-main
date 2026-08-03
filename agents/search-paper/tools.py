"""search-paper 的工具装配：双源搜索 + 去重 + 筛选。"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools.search import (
    ArxivSearchTool, OpenAlexSearchTool, DedupPapersTool, FilterPapersTool,
)

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ArxivSearchTool, OpenAlexSearchTool, DedupPapersTool, FilterPapersTool,
])
