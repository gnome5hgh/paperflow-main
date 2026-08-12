# paperflow/core/security/__init__.py
"""
安全中间件协议层与全部具体中间件——包级统一导出。

协议层定义（ToolContext / SecurityMiddleware / 异常体系）在 ``base.py``：
Python 导入机制规定，当 ``security.py`` 模块与 ``security/`` 包同名共存时，
包总是优先被导入，单独的 ``security.py`` 永远无法以 ``paperflow.core.security``
访问到。把实现放在包内子模块、这里集中导出，可以避免产生无法导入的死代码，
也避免循环导入。具体中间件（审计、工作区路径边界、内容扫描与策略检查）
在 ``middleware/`` 子包，纯工具函数（SSRF 防护、代理字符清洗）保留在顶层。
"""
from paperflow.core.security.base import (
    ConfirmRequired,
    PolicyDenied,
    SecurityBlocked,
    SecurityError,
    SecurityMiddleware,
    ToolContext,
)
from paperflow.core.security.middleware.audit import AuditEntry, AuditMiddleware
from paperflow.core.security.middleware.workspace import (
    WorkspacePolicy,
    WorkspacePolicyMiddleware,
)
from paperflow.core.security.middleware.scanner import (
    SecurityScanMiddleware,
    has_critical,
    scan,
)
from paperflow.core.security.middleware.policy_engine import PolicyEngineMiddleware
from paperflow.core.security.network import SSRFError, resolve_url_target, validate_url_target

__all__ = [
    "ToolContext", "SecurityMiddleware", "SecurityError",
    "PolicyDenied", "ConfirmRequired", "SecurityBlocked",
    "AuditMiddleware", "AuditEntry",
    "WorkspacePolicy", "WorkspacePolicyMiddleware",
    "scan", "has_critical", "SecurityScanMiddleware",
    "PolicyEngineMiddleware",
    "SSRFError", "validate_url_target", "resolve_url_target",
]
