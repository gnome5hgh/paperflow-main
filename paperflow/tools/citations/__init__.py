"""引用管理 LLM 工具面（6 个工具）。

装配面：6 件**全装给 citation-agent**（引用库的读写是它的领域，含 sync_citations /
remove_citation 这两个写入口）；其余角色一件不装，需要查 key、入库、渲染参考文献
时派发它。工具实例经 `make_tools` 装配，`CitationManager` 由装配方注入
（对齐 SpawnSubAgentTool 模式）。
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
