"""引用管理 LLM 工具面（6 个工具）。

装配面：note-agent/research-agent 各装 lookup/add/format/list 四件；review-agent 只装
list+lookup 做溯源核验；sync_citations/remove_citation 仅装配 citation-agent（文献库
维护的唯一写入口）。工具实例经 `make_tools` 装配，`CitationManager` 由装配方
注入（对齐 SpawnSubAgentTool 模式）。
"""
from paperflow.tools.citations.add_citation import AddCitationTool
from paperflow.tools.citations.format_citations import FormatCitationsTool
from paperflow.tools.citations.list_citations import ListCitationsTool
from paperflow.tools.citations.lookup_citation import LookupCitationTool
from paperflow.tools.citations.remove_citation import RemoveCitationTool
from paperflow.tools.citations.sync_citations import SyncCitationsTool

__all__ = [
    "AddCitationTool",
    "FormatCitationsTool",
    "ListCitationsTool",
    "LookupCitationTool",
    "RemoveCitationTool",
    "SyncCitationsTool",
]
