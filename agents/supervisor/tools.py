"""Supervisor 工具装配——只有两个调度工具（派发 + 提问）。

SpawnSubAgentTool 在共享层 paperflow/tools/orchestration/spawn.py 定义,
本文件装配后供 supervisor 使用。Supervisor 是唯一装配 spawn 工具的 agent
(权限最小化:子 agent 不能递归调度)。spawn 结果自带结构化摘要 digest,
supervisor 按交付物类型读各结果组织回答。

记忆工具一件不装:记忆读写全部归 memory-agent,supervisor 要记录只能派发它。
「只调度,不直接执行」因此没有例外——可以直接被断言。
"""
from paperflow.config import PaperFlowConfig
from paperflow.tools.orchestration.spawn import SpawnSubAgentTool
from paperflow.tools.orchestration.ask_user import AskUserQuestionTool


def _make_supervisor_tools() -> list:
    """装配 2 个调度工具。config 在 import 时构造（每进程静态、无副作用）。"""
    cfg = PaperFlowConfig.from_env()
    return [
        SpawnSubAgentTool(agent_timeouts=cfg.agents.timeouts),
        AskUserQuestionTool(),
    ]


# 注：supervisor 工具无 root_hints（无文件访问），无需 make_tools 装配——
# 直接实例化列表即可（AgentRegistry 约定 TOOLS 是 Tool 实例列表）。
TOOLS = _make_supervisor_tools()
