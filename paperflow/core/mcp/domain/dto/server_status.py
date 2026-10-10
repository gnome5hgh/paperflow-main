"""单 server 的观测状态：供 `/mcp` 命令与装配路径读取。"""
from dataclasses import dataclass, field

from paperflow.core.mcp.constants import McpConnectionState
from paperflow.core.mcp.domain.dto.tool_spec import McpToolSpec


@dataclass
class ServerStatus:
    """单 server 观测状态，供 /mcp 命令与装配路径读取。

    Attributes:
        name: str，server 名
        transport: str，传输方式（stdio/http）
        status: McpConnectionState，连接状态：pending | connected | failed
        error: str，失败原因（连接/会话级）
        tools: list[McpToolSpec]，server 报告的工具
        hidden: list[tuple[str, str]]，被过滤掉的工具及原因
        pending_confirm: list[str]，写类中需逐次确认的工具名（write_tools 预批准者不在列）
    """

    name: str
    transport: str
    status: McpConnectionState = McpConnectionState.PENDING
    error: str = ""
    tools: list[McpToolSpec] = field(default_factory=list)
    hidden: list[tuple[str, str]] = field(default_factory=list)
    #: 写类（含缺注解）可见但需逐次确认的工具名；write_tools 预批准者不在列
    pending_confirm: list[str] = field(default_factory=list)

