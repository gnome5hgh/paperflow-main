"""MCP 客户端子系统：config 声明 server → 后台循环持久会话 → 逐工具桥接为原生 Tool。

设计见 spec 2026-10-01-mcp-client-design.md；对外入口：McpClientManager（client.py）、
collect_mcp_agent_tools / build_mcp_tools（bridge.py）。
"""
