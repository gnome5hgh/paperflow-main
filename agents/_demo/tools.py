# agents/_demo/tools.py
"""
Layer 0 验证用 Demo Agent 的工具定义。

``_demo`` Agent 只有一个 echo 工具，用于验证整条链路：
AgentRegistry 扫描 → Agent 加载 Tool → ReAct 循环 → LLM function calling → 工具执行 → 返回结果。

``TOOLS`` 模块级列表是本项目的约定 —— AgentRegistry 通过 importlib
动态加载 tools.py 后读取该列表，因此文件必须定义此变量。
"""

from paperflow.core.tool import Tool, ToolResult


class EchoTool(Tool):
    """
    回显工具 —— Layer 0 最简验证工具。

    接收任意文本消息并原样回显，用于证明：
    - LLM 能正确生成 function call
    - Agent._exec_tool 能正确路由到目标工具
    - ToolResult 能正确拼接到 ReAct 对话流
    """

    #: 工具名称，LLM 通过此名称调用工具
    name = "echo"

    #: 工具描述，LLM 据此判断何时使用此工具
    description = "Echo back the input message"

    #: JSON Schema 参数定义，LLM 据此生成 function arguments
    parameters = {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message to echo",
            }
        },
        "required": ["message"],
    }

    def execute(self, message: str) -> ToolResult:
        """
        执行回显：接收消息，返回带前缀的 ToolResult。

        :param message: LLM 生成的待回显消息
        :returns: ToolResult(text="Echo: <message>")
        """
        return ToolResult(text=f"Echo: {message}")


#: 模块级 Tool 实例列表 —— AgentRegistry 通过此变量发现本 Agent 的所有工具
TOOLS = [EchoTool()]
