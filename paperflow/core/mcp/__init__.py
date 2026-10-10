"""MCP 客户端子系统：config 声明 server → 后台循环持久会话 → 逐工具桥接为原生 Tool。

对外入口：McpClientManager（client.py）、
build_mcp_tools / collect_mcp_agent_tools（bridge.py）。
"""
from paperflow.core.mcp.services.bridge import (  # noqa: F401
    build_mcp_tools,
    collect_mcp_agent_tools,
)
from paperflow.core.mcp.services.client import (  # noqa: F401
    McpClientManager,
    McpToolError,
)
from paperflow.core.mcp.domain import McpToolSpec, ServerStatus  # noqa: F401
