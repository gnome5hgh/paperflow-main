"""searcher 的工具装配：双源搜索 + 文件定位 + 派发 reviewer 做下载门禁。

装配 arxiv/openalex 双源搜索工具(搜索结果自动去重入池)、glob/grep 定位工具,
以及 SpawnSubAgentTool——searcher 用它派发 reviewer 子 agent,对候选论文逐篇
核验「年份/等级/相关性/可下载性」后产出通过清单。spawn 工具需要构造参数
(agent_timeouts),故 make_tools 传已实例化的工具实例而非类;
allowed_spawns 声明放行 reviewer。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools.spawn import SpawnSubAgentTool
from paperflow.tools import ArxivSearchTool, OpenAlexSearchTool, GlobTool, GrepTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ArxivSearchTool, OpenAlexSearchTool, GlobTool, GrepTool,
]) + [SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agent_timeouts)]
