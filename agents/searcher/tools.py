"""searcher 的工具装配：双源搜索（自动去重入池）+ glob/grep 定位 + spawn reviewer 门禁。

spec §2：搜索（A1 年份 + A3 池）→ spawn reviewer（下载审查门禁）→ 下载/推荐。
dedup_papers/filter_papers 已删除：去重并入池插入逻辑（core/search_state.py），
筛选并入 reviewer 下载审查（agents/reviewer）。

spawn 工具与 supervisor/writer 同款（paperflow/tools/spawn.py 共享层）：
需要构造参数（agent_timeouts）故 make_tools 传"已实例化的工具"追加实例而非类；
allowed_spawns: [reviewer] 声明后 _check_spawn_allowed 运行时校验放行 reviewer
子 agent（下载/推荐门禁目标，Task 6）。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.factory import make_tools
from paperflow.tools.spawn import SpawnSubAgentTool
from paperflow.tools import ArxivSearchTool, OpenAlexSearchTool, GlobTool, GrepTool

TOOLS = make_tools(PaperFlowConfig.from_env(), [
    ArxivSearchTool, OpenAlexSearchTool, GlobTool, GrepTool,
]) + [SpawnSubAgentTool(agent_timeouts=PaperFlowConfig.from_env().agent_timeouts)]
