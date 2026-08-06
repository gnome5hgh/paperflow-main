"""search-paper 的工具装配：双源搜索 + 去重 + 筛选 + glob/grep 定位。

Task 4：加 glob/grep（只读搜索）——枚举 vault 内已下载 PDF（决定是否要下载）、
下载前去重核对、下载后内容校验。治 P2 路径风暴：不再盲猜论文精确路径。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools import (
    ArxivSearchTool, OpenAlexSearchTool, DedupPapersTool, FilterPapersTool,
    GlobTool, GrepTool,
)

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ArxivSearchTool, OpenAlexSearchTool, DedupPapersTool, FilterPapersTool,
    GlobTool, GrepTool,
])
