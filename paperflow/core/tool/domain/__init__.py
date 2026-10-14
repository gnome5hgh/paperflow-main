"""Tool 领域的公共词汇：工具契约与结果载体。

按后端分层惯例分两个子包：

- ``entity/`` —— 领域实体：工具这一业务里「一个东西」的模型（`Tool` 抽象与它的安全元数据字段）
- ``dto/``    —— 层间传递的数据载体：工具执行结果（`ToolResult`）

**暂不设 `bo/` 与 `vo/`**：`bo`（领域内中间态，不落库不出站）在本仓尚无 occupant——
`ToolResult.summary` 的结构化载荷是键名契约加 dict，不是类型；`vo`（面向调用方的展示
模型）同样没有——终端侧是 `AgentEvent` 经纯函数映射成活动行，没有视图模型类。将来真
出现第一个 occupant 再建对应子包，不预置空桶。

消费方一律从本包取（``from paperflow.core.tool.domain import Tool, ToolResult``），不关心内部怎么分文件。
"""
from paperflow.core.tool.domain.dto import ToolResult
from paperflow.core.tool.domain.entity import WRITE_EFFECTS, Tool, is_writer

__all__ = [
    # entity
    "Tool",
    "WRITE_EFFECTS",
    "is_writer",
    # dto
    "ToolResult",
]
