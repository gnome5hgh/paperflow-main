"""reviewer 的工具装配：笔记审查与下载审查两模式的工具并集。

两种模式由任务文本前缀分派(SKILL 说明)：
- 「审阅草稿文件…」→ 笔记审查(5 维度审查 + submit_review 交裁决)
- 「审查以下候选论文…」→ 下载审查(lookup_venue_rank 查等级 + submit_download_review)
reviewer 是叶子审稿 agent,不派发子 agent。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
)

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
])
