"""Supervisor 工具装配——Spawn / AskUser + 13 个记忆工具。

SpawnSubAgentTool 在共享层 paperflow/tools/orchestration/spawn.py 定义,
本文件装配后供 supervisor 使用。Supervisor 是唯一装配 spawn 工具的 agent
(权限最小化:子 agent 不能递归调度)。spawn 结果自带结构化摘要 digest,
supervisor 直接读各结果的 digest + needs_attention 组织最终回答。
记忆工具经 paperflow.core.memory.tools 的 get_memory_tools 惰性注入——supervisor
与子 agent 走同一注册表机制,LLM 工具面与旧版一致(同 13 个记忆工具)。
"""
from paperflow.config import PaperFlowConfig
from paperflow.core.memory.tools import get_memory_tools
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools.orchestration.ask_user import AskUserQuestionTool


def _make_supervisor_tools() -> list:
    """装配 2 个调度工具 + 13 个记忆工具。config 在 import 时构造（每进程静态、
    无副作用）；记忆工具经 get_memory_tools 惰性构建，执行时才取运行时上下文。"""
    cfg = PaperFlowConfig.from_env()
    return [
        SpawnSubAgentTool(agent_timeouts=cfg.agent_timeouts),
        AskUserQuestionTool(),
    ] + get_memory_tools()


# 注：supervisor 工具无 allowed_roots（无文件访问），无需 make_tools 装配——
# 直接实例化列表即可（AgentRegistry 约定 TOOLS 是 Tool 实例列表）。
TOOLS = _make_supervisor_tools()
