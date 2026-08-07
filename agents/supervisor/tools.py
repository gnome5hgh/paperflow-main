"""Supervisor 调度工具（spec §3）——保留 AggregateResultsTool / AskUserTool。

SpawnSubAgentTool / ParallelSpawnTool 已抽到共享层 paperflow/tools/spawn.py
（Task 1 纯搬移重构，行为不变）；本文件从共享层 import 后经 _make_supervisor_tools
装配。Supervisor 是唯一装配 spawn 工具的 agent（权限最小化：SubAgent 无递归调度）。
"""
from paperflow.config import PaperFlowConfig
from paperflow.core.tool import Tool, ToolResult
from paperflow.tools.spawn import SpawnSubAgentTool, ParallelSpawnTool


class AggregateResultsTool(Tool):
    """汇总 SubAgentResult 列表；needs_attention 标记呈现（规则 6）。纯文本不做决策。"""

    name = "aggregate_results"
    description = "汇总多个 SubAgentResult 为清晰列表；带 ⚠️ 标记的项需最终呈现给用户。"
    parameters = {
        "type": "object",
        "properties": {
            "results": {"type": "array", "items": {"type": "object"}},
        },
        "required": ["results"],
    }
    # 纯文本汇总，无需 parent 引用
    risk_level = "low"

    def execute(self, results: list[dict]) -> ToolResult:
        lines = []
        for r in results:
            status = r.get("status", "?")
            needs = r.get("needs_attention", False)
            mark = " ⚠️" if needs else ""
            lines.append(f"- [{status}{mark}] {r.get('summary', '')}")
        text = "\n".join(lines) if lines else "(无结果)"
        return ToolResult(text=text)


class AskUserTool(Tool):
    """向用户确认信息（in-turn 阻塞，spec D4②）。

    经 parent.ask_user_callback 读 stdin；callback 为 None（程序化/测试）→
    fail-safe 返回"无法交互"（与 _default_confirm 同款，Supervisor ReAct 自行处理）。
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
        # cb 是 CLI 注入的 stdin 读（worker 线程 input() 可用，spec §3.5 线程注记）
        answer = cb(question)
        return ToolResult(text=f"用户回答：{answer}")


def _make_supervisor_tools() -> list:
    """装配 4 个调度工具。config 在 import 时构造（每进程静态，对齐 make_tools 惯例）；
    agent_timeouts 经 config.yaml 顶层注入 spawn 工具按 agent 解析超时。"""
    cfg = PaperFlowConfig.from_env()
    return [
        SpawnSubAgentTool(agent_timeouts=cfg.agent_timeouts),
        ParallelSpawnTool(agent_timeouts=cfg.agent_timeouts),
        AggregateResultsTool(),
        AskUserTool(),
    ]


# 注：supervisor 工具无 allowed_roots（无文件访问），无需 make_tools 装配——
# 直接实例化列表即可（AgentRegistry 约定 TOOLS 是 Tool 实例列表）。
TOOLS = _make_supervisor_tools()
