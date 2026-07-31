# tests/conftest.py
"""
pytest 共享 fixture 和测试辅助工具。

``MockEchoTool`` 是 EchoTool 的副本，用于在测试中不依赖 agents/_demo/ 目录
即可验证 Tool 执行和 Agent._exec_tool 的路由逻辑。
"""

from paperflow.core.tool import Tool, ToolResult


class MockEchoTool(Tool):
    """
    测试用 EchoTool，与 agents/_demo/tools.py 的 EchoTool 行为完全一致。

    定义在 conftest.py 中以便所有测试文件共享，避免重复定义。
    """

    #: 工具名称，与真实 EchoTool 相同
    name = "echo"

    #: 工具描述
    description = "Echo back the input message"

    #: JSON Schema 参数定义，与真实 EchoTool 相同
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "The message to echo"}
        },
        "required": ["message"],
    }

    def execute(self, message: str) -> ToolResult:
        """
        回显消息，与真实 EchoTool 行为一致。

        :param message: 输入消息
        :returns: ToolResult(text="Echo: <message>")
        """
        return ToolResult(text=f"Echo: {message}")
