"""librarian 的工具装配：引用管理全家桶 + 用户确认 + 读 PDF 首页，不给文件写工具。

文献库维护 agent：bib 的所有写入经 CitationManager（追加/删除/同步都是
托管操作），不需要裸文件工具；不装配 spawn（叶子 agent 不递归调度）、
不装配 RAG 检索（不读论文内容做分析）。read_pdf 只用于元数据缺失时读首页
取标题/作者——读元数据不属于「读论文内容做分析」。
"""
from paperflow.citations import CitationManager
from paperflow.config import PaperFlowConfig
from paperflow.tools.citations import (LookupCitationTool, AddCitationTool,
                                       FormatCitationsTool, ListCitationsTool,
                                       SyncCitationsTool, RemoveCitationTool)
from paperflow.tools import AskUserQuestionTool, ReadPdfTool
from paperflow.tools.common.factory import make_tools


_cm = CitationManager(PaperFlowConfig.from_env())

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    LookupCitationTool(_cm), AddCitationTool(_cm),
    FormatCitationsTool(_cm), ListCitationsTool(_cm),
    SyncCitationsTool(_cm), RemoveCitationTool(_cm),
    ReadPdfTool,                       # 元数据缺失时自己读首页取标题/作者
    AskUserQuestionTool,
])
