"""reviewer 的工具装配：笔记审查、大纲审查与下载审查三模式的工具并集。

三种模式由父 agent spawn 时注入的「当前模式」判别(SKILL 说明)：
- note_review → 笔记审查(5 维度审查 + submit_review 交裁决)
- outline_review → 大纲审查(核验「论点 ← 笔记」映射 + submit_review 交裁决)
- download_review → 下载审查(lookup_venue_rank 查等级 + submit_download_review)
三种模式共用同一工具并集(大纲模式复用笔记模式的 read_file/submit_review)。
reviewer 是叶子审稿 agent,不派发子 agent。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
)

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
])
