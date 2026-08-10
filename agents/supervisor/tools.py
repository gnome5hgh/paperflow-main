"""Supervisor 调度工具装配——Spawn / AskUser。

SpawnSubAgentTool 在共享层 paperflow/tools/spawn.py 定义,
本文件装配后供 supervisor 使用。Supervisor 是唯一装配 spawn 工具的 agent
(权限最小化:子 agent 不能递归调度)。spawn 结果自带结构化摘要 digest,
supervisor 直接读各结果的 digest + needs_attention 组织最终回答。
"""
from paperflow.config import PaperFlowConfig
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.spawn import SpawnSubAgentTool


class AskUserTool(Tool):
    """向用户提问并等待回答(阻塞当前 ReAct 轮)。

    经父 agent 注入的 ask_user_callback 读 stdin;callback 为空(程序化/测试环境)
    时返回"无法交互"提示,由 supervisor 基于已有信息自行决策,不挂死。
    """

    name = "ask_user"
    description = "向用户提问并等待回答（阻塞直到用户输入）。答案作为工具结果返回。"
    parameters = {
        "type": "object",
        "properties": {"question": {"type": "string", "description": "要问用户的问题"}},
        "required": ["question"],
    }
    needs_parent = True
    risk_level = "low"

    def execute(self, question: str) -> ToolResult:
        cb = getattr(self._parent, "ask_user_callback", None)
        if cb is None:
            # fail-safe：无法交互时明确告知，Supervisor 依据已有信息自行决策（不挂死）
            return ToolResult(text="无法交互：当前环境未提供用户回调，请基于已有信息决定")
        # cb 由 CLI 注入,在 worker 线程里读 stdin(阻塞等待用户输入,不冻结事件循环)
        answer = cb(question)
        return ToolResult(text=f"用户回答：{answer}")


def _make_supervisor_tools() -> list:
    """装配 2 个调度工具。config 在 import 时构造(每进程静态、无副作用);
    agent_timeouts 从配置注入,spawn 工具按子 agent 类型解析各自超时。"""
    cfg = PaperFlowConfig.from_env()
    return [
        SpawnSubAgentTool(agent_timeouts=cfg.agent_timeouts),
        AskUserTool(),
    ]


# 注：supervisor 工具无 allowed_roots（无文件访问），无需 make_tools 装配——
# 直接实例化列表即可（AgentRegistry 约定 TOOLS 是 Tool 实例列表）。
TOOLS = _make_supervisor_tools()
