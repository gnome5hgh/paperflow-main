"""mcp 领域模型：桥接契约与观测状态。

连接管理（`client.py`）与逐工具桥接（`bridge.py`）是服务，留在包内各自模块。
"""
from paperflow.core.mcp.domain.dto import McpToolSpec, ServerStatus

__all__ = ["McpToolSpec", "ServerStatus"]
