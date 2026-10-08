"""MCP server 连接状态枚举。"""

from enum import StrEnum

__all__ = ["McpConnectionState"]


class McpConnectionState(StrEnum):
    """一个 MCP server 的连接状态。"""

    #: 首次连接尚未落定（构造时初值）
    PENDING = "pending"
    #: 会话就绪，工具可桥接
    CONNECTED = "connected"
    #: 命令缺失 / 握手失败等；原因写在 ServerStatus.error
    FAILED = "failed"
