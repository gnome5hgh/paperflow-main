"""research-agent 的工具装配：原子文件工具 + 派发 paper-agent/review-agent/rag-agent/citation-agent。

选题发现 agent 自产自写：用 read_pdf 读本地论文 PDF 语料（笔记不进检索知识库、也不参与
溯源，不是本角色的素材），write_file/edit_file
落盘产物（survey/gaps/idea 卡/研究计划）。spawn 工具派发 paper-agent（补料下载、外部
新颖性检索）、review-agent（选题产物审查）、rag-agent（按课题盘点语料——语料检索已收归
它）与 citation-agent（溯源要确认或入库的 key、参考文献渲染——引用库的读写归它，
本角色不装配引用工具）。课题/方向有歧义时把问题写进最终回答（问用户不需要工具）。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools import (
    ReadFileTool, ReadPdfTool, ExtractTitleTool, WriteFileTool, EditFileTool,
    DeleteFileTool, GlobTool, GrepTool,
)
from paperflow.tools.common.factory import make_tools


TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ReadPdfTool, ExtractTitleTool,
    ReadFileTool, WriteFileTool, EditFileTool, DeleteFileTool,
    GlobTool, GrepTool,
    SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agents.timeouts),
], default_write_root="research")
