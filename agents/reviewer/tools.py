"""reviewer 的工具装配：笔记审查（现状）+ 下载审查（新增）两模式并集。

模式由任务文本前缀分派（SKILL 说明）：
- 「审阅草稿文件…」→ 笔记审查（5 维度 + submit_review）
- 「审查以下候选论文…」→ 下载审查（lookup_venue_rank + submit_download_review）
无 spawn：reviewer 是叶子审稿 agent。
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
