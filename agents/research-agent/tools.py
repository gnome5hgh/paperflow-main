"""research-agent 的工具装配：原子文件工具 + 4 引用工具 + 派发 paper-agent/review-agent/rag-agent。

选题发现 agent 自产自写：read_file/read_pdf 读本地笔记与 PDF 语料，write_file/edit_file
落盘产物（survey/gaps/idea 卡/研究计划），引用工具 (lookup/add/format/list) 做溯源标注与
参考文献渲染。spawn 工具派发 paper-agent（补料下载、外部新颖性检索）、review-agent
（plan_review 选题产物审查）与 rag-agent（按课题盘点语料——语料检索已收归它）。
"""
from paperflow.citations import CitationManager
from paperflow.config import PaperFlowConfig
from paperflow.tools.citations import (LookupCitationTool, AddCitationTool,
                                       FormatCitationsTool, ListCitationsTool)
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, WriteFileTool, EditFileTool, DeleteFileTool,
    GlobTool, GrepTool, AskUserQuestionTool,
)
from paperflow.tools.common.factory import make_tools


_cm = CitationManager(PaperFlowConfig.from_env())


TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ReadFileTool, WriteFileTool, EditFileTool, DeleteFileTool,
    GlobTool, GrepTool, AskUserQuestionTool,
    SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agents.timeouts),
    LookupCitationTool(_cm), AddCitationTool(_cm),
    FormatCitationsTool(_cm), ListCitationsTool(_cm),
], default_write_root="research")
