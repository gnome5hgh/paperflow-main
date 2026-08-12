"""searcher 的工具装配：通用 web_search 搜索 + 独立下载 + 文件定位 + 派发 reviewer 门禁。

装配 WebSearchTool（一次调用搜一个 source；多源由 searcher 同一轮并行调用多次，
结果自动去重入池）、FetchPdfTool（对门禁通过的论文下载 PDF）、glob/grep 定位工具、
ask_user_question 交互工具（推荐后询问是否加入未读清单），以及 SpawnSubAgentTool——
searcher 用它派发 reviewer 子 agent，对候选论文逐篇核验「年份/等级/相关性/可下载性」
后产出通过清单。spawn 工具需要构造参数(agent_timeouts)，故 make_tools 传已实例化的
工具实例而非类;allowed_spawns 声明放行 reviewer。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.common.factory import make_tools
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools import (
    WebSearchTool, FetchPdfTool, GlobTool, GrepTool, AskUserQuestionTool,
)

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    WebSearchTool, FetchPdfTool, GlobTool, GrepTool, AskUserQuestionTool,
]) + [SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agent_timeouts)]
