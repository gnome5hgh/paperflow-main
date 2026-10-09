"""paper-agent 的工具装配：论文域的全部工具面——检索交给 MCP（core/mcp）、下载/删除、文件定位、读 PDF 与图表、派发 review-agent 门禁与记账。

FetchPdfTool（对门禁通过的论文下载 PDF）、glob/grep 定位工具、read_pdf（读整篇，
交付材料与溯源）、analyze_figures（图表问题单图分析），以及 SpawnSubAgentTool——paper-agent
用它派发 review-agent（候选论文逐篇核验「年份/等级/相关性/可下载性」）、rag-agent
（入库与收敛索引）与 memory-agent（未读清单与阅读历史）。spawn 工具需要构造参数
(agent_timeouts)，故 make_tools 传已实例化的工具实例而非类。MCP 检索工具由 cli
启动期 collect_mcp_agent_tools 并入（paper-search-mcp 覆盖 arXiv/OpenAlex/Semantic Scholar）。
需要用户拿主意时把问题写进最终回答（问用户不需要工具），不自行猜。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools import (
    FetchPdfTool, DeleteFileTool, GlobTool, GrepTool, ReadPdfTool, ExtractTitleTool,
)
from paperflow.tools.vision.analyze_figures import AnalyzeFiguresTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    FetchPdfTool, DeleteFileTool, GlobTool, GrepTool,
    ReadPdfTool, ExtractTitleTool,
    AnalyzeFiguresTool(),
]) + [SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agents.timeouts)]
