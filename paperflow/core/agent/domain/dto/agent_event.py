"""引擎事件契约：runtime 产出，渲染层与 spawn 遥测消费。"""
from dataclasses import dataclass

from paperflow.core.agent.constants import AgentEventKind


@dataclass
class AgentEvent:
    """引擎事件：kind 取 AgentEventKind；agent_type 区分 root/child。

    载荷按 kind 分家。content 事件装 text（正文增量）；tool 事件装 tool_name 与
    args（工具名与已解析的参数）——**工具事件的 text 是空串**：活动行摘要与调用行
    都由终端层从 args 现算，这里不存排版结果。tool_end 另带耗时、写类 diffstat 与
    结果文本（completion / error）。

    全部可选字段带默认值：旧位置构造（content 事件）兼容不变。

    Attributes:
        kind: AgentEventKind，事件类型（content | tool_start | tool_end）
        text: str，正文增量片段（仅 content 事件；tool 事件为空串）
        agent_type: str，产出事件的 agent（root/child 区分）
        tool_name: str | None，工具名（仅 tool_* 事件）
        args: dict | None，已解析的工具参数（仅 tool_* 事件；终端的展示素材）
        completion: str | None，工具给的完成文本（仅 tool_end）
        error: str | None，错误文本（仅 tool_end；非空即这次调用失败）
        duration_ms: int | None，耗时毫秒（仅 tool_end）
        diffstat: tuple | None，(path, added, removed)，仅写类工具的 tool_end
    """
    kind: AgentEventKind
    text: str
    agent_type: str
    tool_name: str | None = None
    args: dict | None = None
    completion: str | None = None
    error: str | None = None
    duration_ms: int | None = None
    diffstat: tuple | None = None
