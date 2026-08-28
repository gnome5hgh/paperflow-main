"""reviewer 的工具装配：笔记审查、大纲审查与下载审查三模式的工具并集。

三种模式由父 agent spawn 时注入的「当前模式」判别(SKILL 说明)：
- note_review → 笔记审查(5 维度审查 + 溯源核验 + submit_review 交裁决)
- outline_review → 大纲审查(核验「论点 ← 笔记」映射 + 溯源核验 + submit_review 交裁决)
- download_review → 下载审查(lookup_venue_rank 查等级 + submit_download_review)
三种模式共用同一工具并集(大纲模式复用笔记模式的 read_file/submit_review)。
溯源核验工具(list_citations/lookup_citation)挂在共享 manager 上,供笔记/大纲
模式核验 `[来源:key§节]` 的 key 真实性。
reviewer 是叶子审稿 agent,不派发子 agent。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
)
from paperflow.citations import CitationManager
from paperflow.citations.tools import ListCitationsTool, LookupCitationTool

_cm = CitationManager(PaperFlowConfig.from_env())

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
    ListCitationsTool(_cm), LookupCitationTool(_cm),
])
