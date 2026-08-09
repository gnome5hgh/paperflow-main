"""ToolExecutionManager：协调单次工具执行（对应 Letta tool_execution_manager.py）。

paperFlow Agent._exec_tool 负责中间件管道与审计；本类专注「从记忆工具集执行」，
供记忆工具在 agent 工具面外也可独立调用（测试/直接调用路径）。
"""
from __future__ import annotations

from paperflow.core.memory.services.tool_manager import ToolManager
from paperflow.core.tool import ToolResult

__all__ = ["ToolExecutionManager"]


class ToolExecutionManager:
    def __init__(self, tool_manager: ToolManager):
        self.tool_manager = tool_manager

    def execute_tool(self, tool_name: str, args: dict, tool_call_id: str) -> ToolResult:
        tool = self.tool_manager.get_tool(tool_name)
        if tool is None:
            return ToolResult(text=f"Unknown memory tool: {tool_name}")
        try:
            return tool.execute(**args)
        except Exception as e:
            return ToolResult(text=f"Tool error: {e}")
