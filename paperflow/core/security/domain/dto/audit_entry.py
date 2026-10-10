"""一条审计事件的可序列化快照：五类事件共用同一结构，写盘时序列化为一行 JSONL。"""
from dataclasses import dataclass


@dataclass
class AuditEntry:
    """一条审计事件的可序列化快照：五类事件（tool_started / tool_ended /
    approval_requested / approval_decided / llm_call）共用同一结构，按事件
    类型取用不同字段组。写盘时整体 json.dumps 为一行 JSONL。

    Attributes:
        event_type: str，事件类型：tool_started / tool_ended / approval_requested / approval_decided / llm_call
        span_id: str，本次调用/事件的审计 span ID（构建调用树）
        trace_id: str，一次 Agent.run() 的唯一追踪 ID
        session_id: str，会话标识（按对话线程分组）
        agent_type: str，发起调用的 Agent 类型
        tool_name: str，被调用工具名（llm_call 事件为空）
        risk_level: str，工具风险等级（未知工具或 llm_call 为 "unknown"）
        params: dict，脱敏后的调用参数
        parent_id: str | None，父 span ID（None 表示根节点）
        depth: int，调用树深度（根为 0）
        turn: int，ReAct 轮次（从 0 起）
        started_at: str | None，事件开始时间（ISO）
        ended_at: str | None，事件结束时间（ISO）
        policy_decision: str，策略最终决策（auto_allowed/user_confirmed/policy_denied/security_blocked/user_denied/error）
        result_status: str，结果状态（success/policy_blocked/security_blocked/user_denied/error）
        duration_ms: int，执行耗时（毫秒）
        security_scan: dict | None，安全扫描违规明细（仅 SecurityBlocked 场景）
        error: str | None，格式化的错误信息（类型名 + 消息，截断 200 字符）
        result_summary: dict | str | None，结果摘要（优先 summary dict，否则 text 截断 200 字符）
        policy_rules: dict | None，策略评估快照（checked/fired/reason/policy_context）
        approval_outcome: str | None，人工审批结果（user_confirmed/user_denied/auto_denied）
        causation_id: str | None，因果回溯 span ID（decided 指向对应的 requested）
        model: str | None，LLM 模型名（仅 llm_call）
        prompt_tokens: int | None，提示词 token 数（仅 llm_call）
        completion_tokens: int | None，生成 token 数（仅 llm_call）
        total_tokens: int | None，总 token 数（仅 llm_call）
        finish_reason: str | None，结束原因（stop/length 等，仅 llm_call）
    """
    #: 事件类型标识。可选值："tool_started"、"tool_ended"、"approval_requested"、
    #: "approval_decided"、"llm_call"。决定哪些其他字段有效。
    event_type: str
    #: 当前审计跨度（span）的唯一标识符。格式为 "span_" 加 12 位十六进制字符，
    #: 用于构建调用树。每个工具调用或审批/LLM 事件都有独立的 span_id。
    span_id: str
    #: 一次完整 Agent.run() 执行的唯一追踪标识。格式为 "trace_" 加 12 位十六进制字符，
    #: 用于聚合同一次任务中的所有事件（含嵌套调用）。
    trace_id: str
    #: 会话标识，跨多次运行保持一致，用于按对话线程分组审计日志。
    session_id: str
    #: 发起调用的 Agent 类型（如 "supervisor"、"paper-agent"）。
    agent_type: str
    #: 被调用的工具名称。对于 llm_call 事件，该字段为空字符串。
    tool_name: str
    #: 工具的风险等级（来源于 Tool.risk_level），未知工具或 llm_call 时为 "unknown"。
    risk_level: str
    #: 脱敏后的工具调用参数字典。敏感键值（如 api_key、content）被替换为
    #: "***" 或 "HIDDEN"，路径类键（如 path）保留原值。
    params: dict
    #: 父跨度 ID，用于构建调用树。若为 None 则表示当前跨度是根节点（即顶层的工具调用）。
    parent_id: str | None = None
    #: 跨度树中的嵌套深度，根节点为 0，每嵌套一层递增 1。
    depth: int = 0
    #: ReAct 循环轮次索引，从 0 开始计数。用于定位该事件发生在第几轮推理中。
    turn: int = 0
    #: 事件开始时间的 ISO 格式时间戳。
    started_at: str | None = None
    #: 事件结束时间的 ISO 格式时间戳。
    ended_at: str | None = None
    #: 策略引擎的最终决策。由 _derive_decision 推导，可能值：
    #: "auto_allowed"（自动放行）、"user_confirmed"（用户确认放行）、
    #: "policy_denied"（策略拒绝）、"security_blocked"（安全拦截）、
    #: "user_denied"（用户拒绝）、"error"（普通异常）。
    policy_decision: str = ""
    #: 工具执行的结果状态。由 _result_status 推导，可能值：
    #: "success"（成功）、"policy_blocked"（策略阻断）、"security_blocked"（安全阻断）、
    #: "user_denied"（用户拒绝）、"error"（执行异常）。
    result_status: str = ""
    #: 工具执行耗时，单位为毫秒。由 started_at 和 ended_at 计算得出。
    duration_ms: int = 0
    #: 安全扫描违规明细。仅在 SecurityBlocked 场景下有值，
    #: 结构为 {"violations": [{"rule_id": ..., "severity": ..., "snippet": ...}, ...]}。
    security_scan: dict | None = None
    #: 格式化的错误信息（类型名 + 消息，截断至 200 字符）。
    #: 若无错误则为 None。供运维直接定位问题（如超时、404、IO 错误）。
    error: str | None = None
    #: 工具结果的摘要。优先取 result.summary 字典（已脱敏），
    #: 否则取 result.text 字段，去除换行后截断至 200 字符。
    result_summary: dict | str | None = None
    #: 策略评估的快照。包含 checked（检查项列表）、fired（命中的规则名）、
    #: reason（拒绝/拦截原因）、policy_context（当时策略配置输入）。
    #: 用于事后 replay 当时决策依据。
    policy_rules: dict | None = None
    #: 人工审批结果。仅在 approval_decided 或 tool_ended 场景下有效，
    #: 可能值："user_confirmed"（用户放行）、"user_denied"（用户拒绝）、
    #: "auto_denied"（无人值守默认拒绝）。
    approval_outcome: str | None = None
    #: 用于跨事件回溯的跨度 ID。在 approval_decided 事件中指向对应的
    #: approval_requested 事件，实现"请求 ≠ 决策"的因果关联。
    causation_id: str | None = None

    # ---------- 以下字段仅对 llm_call 事件有效，其他事件类型为空 ----------
    #: LLM 模型名称（仅 llm_call 事件使用）。
    model: str | None = None
    #: 提示词消耗的 Token 数量（仅 llm_call 事件使用）。
    prompt_tokens: int | None = None
    #: 生成回复消耗的 Token 数量（仅 llm_call 事件使用）。
    completion_tokens: int | None = None
    #: 本次 LLM 调用的总 Token 数量 = prompt_tokens + completion_tokens（仅 llm_call 事件使用）。
    total_tokens: int | None = None
    #: LLM 的结束原因，如 "stop"（正常结束）、"length"（超过 max_tokens 截断）等（仅 llm_call 事件使用）。
    finish_reason: str | None = None

