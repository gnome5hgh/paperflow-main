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

    Attributes:
        trace_id: str，本次 run 的唯一追踪 ID（聚合一次任务的所有工具调用）
        session_id: str，会话标识（跨多次 run 保持一致）
        agent_type: str，当前 Agent 类型（如 supervisor/searcher）
        tool: Tool | None，当前执行的 Tool；None 表示未知工具（幻觉或注入）
        tool_name: str，工具名（tool 为 None 时仍可记录）
        args: dict，已解析的工具参数字典
        timestamp: str | None，ISO 格式的调用发起时间（审计的 started_at）
        started_at: float | None，time.monotonic() 起始时刻，用于算耗时
        result: ToolResult | None，工具执行成功后的结果
        error: Exception | None，执行或中间件拦截时抛出的异常
        user_confirmed: bool，是否已获用户确认
        diffstat_old: str | None，写类工具执行前采样的旧文本（仅渲染遥测，中间件不读）
        turn: int，ReAct 循环轮次（从 0 起）
        span_id: str | None，本次调用的审计 span ID（由 AuditMiddleware 生成）
        parent_id: str | None，父 span ID（嵌套调用时取栈顶）
        depth: int，调用深度（根为 0，嵌套每层 +1）
        policy_context: dict | None，策略引擎评估时的配置快照
        policy_fired: str | None，被触发的策略规则名（如 blocked_by_default）
        approval_outcome: str | None，审批最终结果（user_confirmed/user_denied/auto_denied）
        approval_decided_span_id: str | None，审批决策事件的 span ID（审计回溯用）
    """

    # --- 调用方与工具本体（由 Agent 构造，贯穿管道） ---
    trace_id: str                      # 本次 run 的唯一追踪 ID，用于聚合一次完整任务的所有工具调用
    session_id: str                    # 会话标识，跨多次 run 保持一致，便于审计聚合
    agent_type: str                    # 当前 Agent 的类型（如 "supervisor"、"searcher"）
    tool: Tool | None = None           # 当前执行的 Tool 实例；None 表示未知工具（LLM 幻觉或注入）
    tool_name: str = ""                # 工具名称（用于审计，即使 tool 为 None 也能记录）
    args: dict = field(default_factory=dict)  # 工具调用的参数字典（已解析 JSON）

    # --- 生命周期时间戳（由 Agent 在调用前后记录） ---
    timestamp: str | None = None       # ISO 格式的调用发起时间（用于审计的 started_at）
    started_at: float | None = None    # time.monotonic() 记录的起始时刻，用于计算耗时

    # --- 执行结果与异常（由 Agent 填充） ---
    result: ToolResult | None = None   # 工具执行成功后的结果对象
    error: Exception | None = None     # 工具执行或中间件拦截时抛出的异常

    # --- 用户确认状态（由 Agent 在确认流程中设置） ---
    user_confirmed: bool = False       # 是否已获得用户确认（仅对需要确认的工具有效）

    diffstat_old: str | None = None   # 仅渲染遥测：写类工具执行前采样的旧文本，
                                      # Agent 填充；中间件不读、审计不含

    # --- 审计树与决策信息（由各中间件分别填充） ---
    turn: int = 0                      # ReAct 循环的轮次（从 0 开始）
    span_id: str | None = None         # 当前工具调用的审计 span ID（由 AuditMiddleware 生成）
    parent_id: str | None = None       # 父 span ID（嵌套调用时，由 AuditMiddleware 从栈顶读取）
    depth: int = 0                     # 调用深度（根为 0，嵌套每层 +1）

    policy_context: dict | None = None # 策略引擎评估时的配置快照（由 PolicyEngineMiddleware 记录）
    policy_fired: str | None = None    # 被触发的策略规则名（如 "blocked_by_default"）
    approval_outcome: str | None = None # 审批最终结果（"user_confirmed" / "user_denied" / "auto_denied"）
    approval_decided_span_id: str | None = None  # 审批决策事件的 span ID（用于审计回溯）


class SecurityMiddleware(ABC):
    """安全中间件基类：定义四个钩子（before / after / on_finish / on_approval）。

    装配后按洋葱模型执行：before 按注册顺序执行（可抛异常拦截），工具执行后
    after 逆序收口（后注册的中间件先看到结果），整轮对话收尾跑 on_finish。
    具体中间件只覆写自己关心的钩子，其余保持基类默认空实现。
    """

    async def before(self, ctx: ToolContext) -> None:
        """工具执行前的钩子：可在此拦截（抛异常）或记录请求。默认空实现。

        实现者可以：
            - 检查 ctx.args、ctx.tool 等字段；
            - 修改 ctx 中的内容（如添加额外元数据）；
            - 抛出 SecurityError 子类（PolicyDenied / ConfirmRequired / SecurityBlocked）来阻止执行；
            - 正常返回则继续下一个中间件。

        Args:
            ctx: 当前工具调用的上下文，各字段在管道中逐步填充。
        """
        return

    async def after(self, ctx: ToolContext) -> None:
        """工具执行后的钩子：可检查结果、补做审计或改写输出。

        所有路径都会走到本钩子——含 JSON 解析失败、未知工具、被 before 拦截的
        早退场景，因此审计能覆盖每一次调用。默认空实现。

        实现者可以通过 ctx.error 判断是否发生了异常，并通过 ctx.result 访问执行结果。
        注意：after 逆序执行（后注册的中间件先执行），形成洋葱模型。

        Args:
            ctx: 工具调用的完整上下文，包含执行结果或异常信息。
        """
        return

    async def on_finish(self, agent, content: str) -> str:
        """整轮对话收尾时的钩子：可在最终回复落定前改写内容。默认原样返回。

        在 ReAct 循环最终产出无 tool_calls 的 assistant 消息后，所有中间件按注册顺序
        依次执行本钩子，每个中间件都可以对最终内容做改写（如追加来源引用、替换不安全内容）。
        改写后的内容将作为本轮 run 的返回值交付给调用方，并持久化到对话历史。

        Args:
            agent: 当前 Agent 实例（可用于访问其属性，如 session_id 等）
            content: 当前累计的最终回复文本（可能已被前面的中间件改写）

        Returns:
            str: 改写后的最终回复文本
        """
        return content

    async def on_approval(self, ctx: ToolContext, phase: str, approval_outcome: str | None = None) -> None:
        """审批生命周期钩子：phase ∈ {"requested", "decided"}，仅 AuditMiddleware 实现。

        其余中间件默认 no-op——这是洋葱模型外的可选横切关注点，只给需要监听
        确认流程的中间件用（当前只有审计要落盘审批双事件）。

        当需要用户确认的工具触发 ConfirmRequired 时，Agent 会在发送确认请求前
        调用所有中间件的 on_approval(ctx, "requested")，然后在收到用户决策后
        调用 on_approval(ctx, "decided", approval_outcome)。

        Args:
            ctx: 工具调用的上下文（此时尚未执行工具）
            phase: "requested" 或 "decided"
            approval_outcome: 仅当 phase == "decided" 时有效，值为 "user_confirmed"、
                "user_denied" 或 "auto_denied"（fail-safe 默认拒绝）。
        """
        return


class SecurityError(Exception):
    """安全相关异常的基类；decision 字段标识安全决策类型，供上层区分处理。

    WHY 用字段而非 isinstance 链：审计写 tool_ended 时一行 ``getattr`` 就能
    推导出决策类型，不需要维护一套「异常 → 标签」的映射表；普通工具异常
    （RuntimeError 等）没有该属性，兜底取 "error"。

    Attributes:
        decision: str，安全决策类型（子类覆盖为具体值，如 policy_denied）
    """

    decision: str   # 子类必须覆盖为具体的决策字符串，如 "policy_denied"


class PolicyDenied(SecurityError):
    """策略拒绝：工具被策略检查判定为不可执行，携带拒绝原因。

    Attributes:
        decision: str，固定为 "policy_denied"
        reason: str，人类可读的拒绝理由（反馈给 LLM）
    """

    decision = "policy_denied"

    def __init__(self, reason: str):
        """记录拒绝理由。

        Args:
            reason: str，拒绝理由；写入实例供上层反馈给 LLM
        """
        self.reason = reason   # 人类可读的拒绝理由，将反馈给 LLM


class ConfirmRequired(SecurityError):
    """需要确认：工具风险较高，等待用户确认后才能继续执行。

    携带工具名、入参、风险等级、副作用说明与确认回调；用户放行后调用
    ``confirm()`` 触发回调，把本次确认记进已确认集合（见 PolicyEngineMiddleware）。
    未确认（异常未放行）一律视为拒绝——只有最终放行的调用才算允许。

    Attributes:
        decision: str，固定为 "confirm_required"
        tool_name: str，需要用户确认的工具名
        params: dict，本次调用的参数（展示给用户）
        risk_level: str，工具风险等级
        side_effects: str，可能的副作用说明（如「写入文件」）
        _on_confirmed: 回调 | None，确认后触发（把本次确认记入已确认集合）
    """

    decision = "confirm_required"

    def __init__(self, tool_name, params, risk_level, side_effects, on_confirmed=None):
        """记录待确认工具的展示信息与放行回调。

        Args:
            tool_name: str，工具名
            params: dict，本次调用参数（展示给用户）
            risk_level: str，工具风险等级
            side_effects: str，副作用说明
            on_confirmed: 回调 | None，用户放行后触发，用于记入已确认集合
        """
        self.tool_name = tool_name          # 需要用户确认的工具名
        self.params = params                # 本次调用的参数（用于展示给用户）
        self.risk_level = risk_level        # 工具的风险等级（如 "high"）
        self.side_effects = side_effects    # 工具可能产生的副作用说明（如 "写入文件"）
        self._on_confirmed = on_confirmed   # 确认后触发的回调（由 PolicyEngineMiddleware 传入，用于更新已确认集合）

    def confirm(self) -> None:
        """触发确认回调，放行该工具后续执行；未设置回调时为空操作。

        用户确认后由 Agent 调用此方法，将本次工具调用标记为“已确认”，
        避免同一 (工具, 目标路径) 重复询问。
        """
        if self._on_confirmed:
            self._on_confirmed()


class SecurityBlocked(SecurityError):
    """安全拦截：内容或路径检查发现违规，携带违规明细列表。

    Attributes:
        decision: str，固定为 "security_blocked"
        reason: str，拦截原因摘要
        violations: list[dict]，违规明细（每项含 rule_id/severity/snippet）
    """

    decision = "security_blocked"

    def __init__(self, reason: str, violations: list[dict]):
        """记录拦截原因与违规明细。

        Args:
            reason: str，拦截原因摘要
            violations: list[dict]，违规明细列表
        """
        self.reason = reason                # 拦截原因摘要
        self.violations = violations        # 违规明细列表，每个元素包含 rule_id、severity、snippet 等