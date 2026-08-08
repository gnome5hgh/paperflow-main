# paperflow/core/security/__init__.py
"""
安全中间件协议层：定义工具调用的上下文对象、安全中间件的三个钩子与异常体系。

本模块直接写在包入口文件里，而不是独立的 ``security.py``：Python 导入机制
规定，当 ``security.py`` 模块与 ``security/`` 包同名共存时，包总是优先被
导入，单独的 ``security.py`` 永远无法以 ``paperflow.core.security`` 访问到。
把实现统一放在包内，可以避免产生无法导入的死代码，也避免循环导入。

包内还包含若干具体的安全中间件子模块：审计、工作区路径边界、内容扫描与
策略检查。
"""

from abc import ABC
from dataclasses import dataclass, field

from paperflow.core.tool import Tool, ToolResult


@dataclass
class ToolContext:
    """一次工具调用的上下文快照：调用方信息、入参、结果与异常，供各中间件检查与审计。"""

    trace_id: str
    session_id: str
    agent_type: str
    tool: Tool | None = None       # None 表示未知工具（大模型幻觉或注入），此时仍会走 after 钩子审计
    tool_name: str = ""
    args: dict = field(default_factory=dict)
    timestamp: str | None = None
    started_at: float | None = None
    result: ToolResult | None = None
    error: Exception | None = None
    user_confirmed: bool = False


class SecurityMiddleware(ABC):
    """安全中间件基类：定义三个钩子，具体中间件按需覆写其中若干。"""

    async def before(self, ctx: ToolContext) -> None:
        """工具执行前的钩子：可在此拦截（抛异常）或记录请求。默认空实现。"""
        return

    async def after(self, ctx: ToolContext) -> None:
        """工具执行后的钩子：可检查结果、补做审计或改写输出。默认空实现。"""
        return

    async def on_finish(self, agent, content: str) -> str:
        """整轮对话收尾时的钩子：可在最终回复落定前改写内容。默认原样返回。"""
        return content


class SecurityError(Exception):
    """安全相关异常的基类；decision 字段标识安全决策类型，供上层区分处理。"""

    decision: str


class PolicyDenied(SecurityError):
    """策略拒绝：工具被策略检查判定为不可执行。"""

    decision = "policy_denied"

    def __init__(self, reason: str):
        self.reason = reason


class ConfirmRequired(SecurityError):
    """需要确认：工具风险较高，等待用户确认后才能继续执行。"""

    decision = "confirm_required"

    def __init__(self, tool_name, params, risk_level, side_effects, on_confirmed=None):
        self.tool_name = tool_name
        self.params = params
        self.risk_level = risk_level
        self.side_effects = side_effects
        self._on_confirmed = on_confirmed

    def confirm(self) -> None:
        """触发确认回调，放行该工具后续执行；未设置回调时为空操作。"""
        if self._on_confirmed:
            self._on_confirmed()


class SecurityBlocked(SecurityError):
    """安全拦截：内容或路径检查发现违规，携带违规明细列表。"""

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
