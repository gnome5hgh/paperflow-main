"""MCP 客户端平台的跨模块词汇。

状态供 `/mcp` 命令展示，也决定工具装配门禁——只有已连接的 server 才桥接工具。
枚举在 `enums.py`；本包只做再导出，消费方一律从 `paperflow.core.mcp.constants` 取。
"""

from .enums import McpConnectionState

__all__ = ["McpConnectionState"]
