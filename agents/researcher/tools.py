"""researcher 的工具装配：原子文件工具 + RAG 语料盘点 + 4 引用工具 + 派发 searcher/reviewer。

选题发现 agent 自产自写：read_file/read_pdf 读本地笔记与 PDF 语料，rag_retrieve 按课题
发现相关段落，write_file/edit_file 落盘产物（survey/gaps/idea 卡/研究计划），引用工具
(lookup/add/format/list)做溯源标注与参考文献渲染。spawn 工具派发 searcher（补料下载、
外部新颖性检索）与 reviewer（plan_review 选题产物审查）。
"""
from paperflow.citations import CitationManager
from paperflow.config import PaperFlowConfig
from paperflow.tools.citations import (LookupCitationTool, AddCitationTool,
                                       FormatCitationsTool, ListCitationsTool)
from paperflow.tools.rag import RagRetrieveTool
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, WriteFileTool, EditFileTool,
    GlobTool, GrepTool, AskUserQuestionTool,
)
from paperflow.tools.common.factory import make_tools


_cm = CitationManager(PaperFlowConfig.from_env())


TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ReadFileTool, WriteFileTool, EditFileTool,
    RagRetrieveTool, GlobTool, GrepTool, AskUserQuestionTool,
    SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agent_timeouts),
    LookupCitationTool(_cm), AddCitationTool(_cm),
    FormatCitationsTool(_cm), ListCitationsTool(_cm),
], default_write_root="research")
