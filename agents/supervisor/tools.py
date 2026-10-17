"""Supervisor 工具装配——调度工具 + 只读召回。

SpawnSubAgentTool 在共享层 paperflow/tools/orchestration/spawn.py 定义,
本文件装配后供 supervisor 使用。Supervisor 是唯一装配 spawn 工具的 agent
(权限最小化:子 agent 不能递归调度)。spawn 结果自带结构化摘要 digest,
supervisor 按交付物类型读各结果组织回答。

**问用户不需要工具**:需要用户给信息时,把问题写进最终回答、本轮结束;
交互时用户下一行输入就是下一轮的输入。因此这里没有提问工具。

记忆写入工具一件不装:记忆读写全部归 memory-agent,supervisor 要记录只能派发它。
只读召回（conversation_search / read_tool_result）是例外：它不产生领域产物，
只是核对原文，装配给所有需要确认细节的 agent。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools.recall import get_recall_tools


def _make_supervisor_tools() -> list:
    """装配调度工具与只读召回。config 在 import 时构造（每进程静态、无副作用）。"""
    cfg = PaperFlowConfig.from_env()
    return [
        SpawnSubAgentTool(agent_timeouts=cfg.agents.timeouts, max_turns=cfg.agents.max_turns),
    ]


# 注：supervisor 工具无 root_hints（无文件访问），无需 make_tools 装配——
# 直接实例化列表即可（AgentRegistry 约定 TOOLS 是 Tool 实例列表）。
TOOLS = _make_supervisor_tools() + get_recall_tools()
