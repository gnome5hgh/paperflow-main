"""review-agent 的工具装配：笔记审查、下载审查与研究选题产物审查三种模式的工具并集。

三种审查各是一份 skill(review-note / review-plan / review-download),开审前按任务加载：
- note_review → 笔记审查(5 维度审查 + 溯源核验 + submit_review 交裁决)
- download_review → 下载审查(lookup_venue_rank 查等级 + submit_download_review)
- plan_review → 研究选题产物审查(四产物交叉核验 + 溯源标注 + 素材熔断诚实性,
  submit_review 交裁决)
三种模式共用同一工具并集。
溯源核验工具(list_citations/lookup_citation)挂在共享 manager 上,供笔记/选题产物
审查模式核验 `[来源:key§节]` 的 key 真实性。
review-agent 是叶子审稿 agent,不派发子 agent。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
)
from paperflow.citations import CitationManager
from paperflow.tools.citations import ListCitationsTool, LookupCitationTool

_cm = CitationManager(PaperFlowConfig.from_env())

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
    ListCitationsTool(_cm), LookupCitationTool(_cm),
])
