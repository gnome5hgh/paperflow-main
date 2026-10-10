"""桥接前的 MCP 工具描述：从 SDK 类型剥离的最小面（单测不依赖 mcp 包）。"""
from dataclasses import dataclass


@dataclass
class McpToolSpec:
    """桥接前的 MCP 工具描述——从 SDK 类型剥离的最小面，单测不依赖 mcp 包。

    Attributes:
        name: str，MCP 工具名（server 原始名，未加前缀）
        description: str，工具描述（模型判断何时使用）
        input_schema: dict | None，MCP inputSchema 原文
        annotations: dict | None，MCP annotations（readOnlyHint 等，缺省按「可能写」处理）
    """

    name: str
    description: str
    input_schema: dict | None
    annotations: dict | None

