# paperflow/core/security/base.py
"""
安全中间件协议层：定义工具调用的上下文对象、安全中间件的四个钩子与异常体系。

协议层放在独立子模块而不是包入口：Python 导入机制规定，当 ``security.py``
模块与 ``security/`` 包同名共存时，包总是优先被导入，单独的 ``security.py``
永远无法以 ``paperflow.core.security`` 访问到。把实现放在包内子模块、由包
入口统一导出，可以避免产生无法导入的死代码，也避免中间件子模块回引包入口
时的循环导入。
"""

from abc import ABC
from dataclasses import dataclass, field

from paperflow.core.tool import Tool, ToolResult


@dataclass
class ToolContext:
    """一次工具调用的上下文快照：调用方信息、入参与结果异常，供各中间件检查与审计。

    字段按来源分四组：调用方与工具本体（trace/session/agent/tool/args）、生命周期
    （timestamp/started_at）、结果与决策（result/error/user_confirmed/policy_*/
    approval_*）、审计树（turn/span_id/parent_id/depth）。各组由不同中间件在各自
    钩子里填充——见下方字段注释。``tool=None`` 表示未知工具（大模型幻觉或注入），
    此时各 before 钩子自行跳过，仍会走 after 钩子审计。
    """

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
    # 审计树与决策字段：turn = ReAct 轮次；span_id/parent_id/depth 由
    # AuditMiddleware.before 通过 contextvar 填充；policy_context/policy_fired 由
    # PolicyEngineMiddleware 填充；approval_* 由 Agent 在确认流程里填充。
    turn: int = 0
    span_id: str | None = None
    parent_id: str | None = None
    depth: int = 0
    policy_context: dict | None = None
    policy_fired: str | None = None
    approval_outcome: str | None = None
    approval_decided_span_id: str | None = None


class SecurityMiddleware(ABC):
    """安全中间件基类：定义四个钩子（before / after / on_finish / on_approval）。

    装配后按洋葱模型执行：before 按注册顺序执行（可抛异常拦截），工具执行后
    after 逆序收口（后注册的中间件先看到结果），整轮对话收尾跑 on_finish。
    具体中间件只覆写自己关心的钩子，其余保持基类默认空实现。
    """

    async def before(self, ctx: ToolContext) -> None:
        """工具执行前的钩子：可在此拦截（抛异常）或记录请求。默认空实现。"""
        return

    async def after(self, ctx: ToolContext) -> None:
        """工具执行后的钩子：可检查结果、补做审计或改写输出。

        所有路径都会走到本钩子——含 JSON 解析失败、未知工具、被 before 拦截的
        早退场景，因此审计能覆盖每一次调用。默认空实现。
        """
        return

    async def on_finish(self, agent, content: str) -> str:
        """整轮对话收尾时的钩子：可在最终回复落定前改写内容。默认原样返回。"""
        return content

    async def on_approval(self, ctx: ToolContext, phase: str, approval_outcome: str | None = None) -> None:
        """审批生命周期钩子：phase ∈ {"requested", "decided"}，仅 AuditMiddleware 实现。

        其余中间件默认 no-op——这是洋葱模型外的可选横切关注点，只给需要监听
        确认流程的中间件用（当前只有审计要落盘审批双事件）。
        """
        return


class SecurityError(Exception):
    """安全相关异常的基类；decision 字段标识安全决策类型，供上层区分处理。

    WHY 用字段而非 isinstance 链：审计写 tool_ended 时一行 ``getattr`` 就能
    推导出决策类型，不需要维护一套「异常 → 标签」的映射表；普通工具异常
    （RuntimeError 等）没有该属性，兜底取 "error"。
    """

    decision: str


class PolicyDenied(SecurityError):
    """策略拒绝：工具被策略检查判定为不可执行，携带拒绝原因。"""

    decision = "policy_denied"

    def __init__(self, reason: str):
        self.reason = reason


class ConfirmRequired(SecurityError):
    """需要确认：工具风险较高，等待用户确认后才能继续执行。

    携带工具名、入参、风险等级、副作用说明与确认回调；用户放行后调用
    ``confirm()`` 触发回调，把本次确认记进已确认集合（见 PolicyEngineMiddleware）。
    未确认（异常未放行）一律视为拒绝——只有最终放行的调用才算允许。
    """

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
