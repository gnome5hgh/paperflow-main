"""review-agent 的工具装配：三类审查的共用工具并集 + 派发 citation-agent 做溯源核验。

三类审查各是一份 skill(review-note / review-plan / review-download),开审前按任务加载：
- review-note：笔记审查(五维审查 + 溯源核验 + submit_review 交裁决)
- review-download：下载审查(lookup_venue_rank 查等级 + submit_download_review)
- review-plan：研究选题产物审查(四产物交叉核验 + 溯源标注 + 素材熔断诚实性,
  submit_review 交裁决)
三份流程共用同一工具并集。

溯源核验(核 `[来源:key§节]` / `**论文引用**` 的 key 是否真实存在于 references.bib)
派发 citation-agent——引用库的读写归它,本角色不装配引用工具,也不信任标注本身。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
)
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadFileTool, ReadPdfTool, FormatCheckTool, SubmitReviewTool,
    LookupVenueRankTool, SubmitDownloadReviewTool, GlobTool, GrepTool,
    SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agents.timeouts),
])
