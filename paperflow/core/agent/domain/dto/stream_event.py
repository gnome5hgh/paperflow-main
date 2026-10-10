"""流式事件契约：runtime 产出，渲染层与 spawn 遥测消费。"""
from dataclasses import dataclass


@dataclass
class StreamEvent:
    """流式事件：kind ∈ {"content","tool_start","tool_end"}；text 为片段；agent_type 区分 root/child。

    结构化字段仅 tool_* 事件携带：tool_name/summary（start+end 都有）、
    duration_ms/diffstat（仅 end；diffstat=(path, added, removed)，仅写类工具）。
    全部带默认值：旧位置构造（content 事件）兼容不变。

    Attributes:
        kind: str，事件类型：content | tool_start | tool_end
        text: str，文本片段（content 事件为增量内容）
        agent_type: str，产出事件的 agent（root/child 区分）
        tool_name: str | None，工具名（仅 tool_* 事件）
        summary: str | None，活动行摘要（仅 tool_* 事件）
        duration_ms: int | None，耗时毫秒（仅 tool_end）
        diffstat: tuple | None，(path, added, removed)，仅写类工具的 tool_end
    """
    kind: str
    text: str
    agent_type: str
    tool_name: str | None = None
    summary: str | None = None
    duration_ms: int | None = None
    diffstat: tuple | None = None

