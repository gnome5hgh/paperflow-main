# paperflow/core/security/__init__.py
"""
安全中间件协议层：ToolContext、SecurityMiddleware 三钩子、异常体系。

.. note::

    本实现直接位于包 __init__.py 内：Python 导入机制中，当同名
    ``security.py`` 模块与 ``security/`` 包共存时，包总是优先，
    ``security.py`` 永远无法以 ``paperflow.core.security`` 被导入，
    因此实现统一放在包内，避免死代码与循环导入。
    后续中间件（Audit/WorkspacePolicy/SecurityScan/PolicyEngine）
    作为本包的子模块追加。
"""

from abc import ABC
from dataclasses import dataclass

from paperflow.core.tool import Tool, ToolResult


@dataclass
class ToolContext:
    trace_id: str
    session_id: str
    agent_type: str
    tool: Tool
    tool_name: str
    args: dict
    timestamp: str | None = None
    started_at: float | None = None
    result: ToolResult | None = None
    error: Exception | None = None
    user_confirmed: bool = False


class SecurityMiddleware(ABC):
    async def before(self, ctx: ToolContext) -> None:
        return

    async def after(self, ctx: ToolContext) -> None:
        return

    async def on_finish(self, agent, content: str) -> str:
        return content


class SecurityError(Exception):
    decision: str


class PolicyDenied(SecurityError):
    decision = "policy_denied"

    def __init__(self, reason: str):
        self.reason = reason


class ConfirmRequired(SecurityError):
    decision = "confirm_required"

    def __init__(self, tool_name, params, risk_level, side_effects, on_confirmed=None):
        self.tool_name = tool_name
        self.params = params
        self.risk_level = risk_level
        self.side_effects = side_effects
        self._on_confirmed = on_confirmed

    def confirm(self) -> None:
        if self._on_confirmed:
            self._on_confirmed()


class SecurityBlocked(SecurityError):
    decision = "security_blocked"

    def __init__(self, reason: str, violations: list[dict]):
        self.reason = reason
        self.violations = violations


# 注意：audit 子模块依赖本模块的异常/中间件类，须在类定义之后导入，
# 否则触发 "partially initialized module" 循环导入错误
from paperflow.core.security.audit import AuditMiddleware, AuditEntry
from paperflow.core.security.workspace import WorkspacePolicy, WorkspacePolicyMiddleware
from paperflow.core.security.scanner import scan, has_critical, SecurityScanMiddleware
from paperflow.core.security.policy_engine import PolicyEngineMiddleware
from paperflow.core.security.network import SSRFError, validate_url_target, resolve_url_target

__all__ = [
    "ToolContext", "SecurityMiddleware", "SecurityError",
    "PolicyDenied", "ConfirmRequired", "SecurityBlocked",
    "AuditMiddleware", "AuditEntry",
    "WorkspacePolicy", "WorkspacePolicyMiddleware",
    "scan", "has_critical", "SecurityScanMiddleware",
    "PolicyEngineMiddleware",
    "SSRFError", "validate_url_target", "resolve_url_target",
]
