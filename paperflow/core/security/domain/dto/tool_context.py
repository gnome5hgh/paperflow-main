"""一次工具调用的上下文快照：调用方信息、入参与结果异常，供各中间件检查与审计。"""
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
        agent_type: str，当前 Agent 类型（如 supervisor/paper-agent）
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
    agent_type: str                    # 当前 Agent 的类型（如 "supervisor"、"paper-agent"）
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

