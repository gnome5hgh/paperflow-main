"""Tool 领域的层间数据载体（dto）。

工具执行的结果对象（`ToolResult`）：把「正文 + 结构化摘要」从工具层搬到引擎层与
模型层。与 `entity/` 的分界是「是不是这个业务里的东西本身」——这里只回答
「数据长什么样」。
"""
from paperflow.core.tool.domain.dto.tool_result import ToolResult

__all__ = ["ToolResult"]
