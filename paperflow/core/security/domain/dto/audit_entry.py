"""一条审计事件的可序列化快照：各类事件共用同一结构，写盘时序列化为一行 JSONL。

字段名对齐业界标准词汇（W3C Trace Context / OTel GenAI / OpenInference）：
标识三元组按 W3C 宽度（trace 32-hex / span 16-hex）；LLM 调用属性用
``gen_ai.*``；数据类字段（input/output）取 OpenInference 的语义。dataclass
属性名用 Python 标识符，落盘时经 ``to_json_dict()`` 映射为标准 JSON 键
（含点号），因此消费方读到的就是标准名。
"""
from dataclasses import dataclass
from typing import Any, ClassVar


@dataclass
class AuditEntry:
    """一条审计事件的可序列化快照。事件类型由 ``event_type`` 判别（履行
    OpenInference ``span.kind`` 的职责），按类型取用不同字段组。写盘时整体
    经 ``to_json_dict()`` 序列化为一行 JSONL。

    事件类型：
    - ``tool_started`` / ``tool_ended``：工具调用的 span 起止（成对落盘，
      started 无配对 ended 即中断——崩溃检测不变式）；
    - ``approval_requested`` / ``approval_decided``：审批请求与决策两条独立
      事件，decided 经 ``causation_id`` 回溯 requested（请求 ≠ 决策）；
    - ``agent_started``：agent 实例起跑（原始 task / 配置版本 / 模型）；
    - ``llm_call``：LLM 调用元数据（模型 / token 数 / 耗时），不记 prompt 正文。

    Attributes:
        event_type: str，事件类型判别器（见上）
        span_id: str，本次调用/事件的审计 span ID（W3C 64-bit，16 hex，构建调用树）
        trace_id: str，一次 Agent.run() 的唯一追踪 ID（W3C 128-bit，32 hex）
        session_id: str，会话标识（按对话线程分组）
        agent_type: str，发起调用的 Agent 类型
        tool_name: str，被调用工具名（非工具事件为空）
        risk_level: str，工具风险档位（未知工具或非工具事件为 "unknown"/""）
        input: Any，脱敏后的调用参数（凭证类键替换为 "***"，路径类保留原值）
        parent_span_id: str | None，父 span ID（None 表示根节点）
        turn: int，ReAct 轮次（从 0 起）
        started_at: str | None，事件开始时间（ISO）
        ended_at: str | None，事件结束时间（ISO）
        policy_decision: str，策略最终决策（ToolDecision 的取值）
        result_status: str，结果状态（ToolResultStatus 的取值）
        duration_ms: int，执行耗时（毫秒）
        security_scan: dict | None，安全扫描违规明细（仅 SecurityBlocked 场景）
        error: str | None，格式化的错误信息（类型名 + 消息，截断 200 字符）
        output: Any，工具返回的**全量原文**（取证回放用，不截断）
        summary: dict | str | None，结构化摘要通道（spawn digest / RAG documents 等结构化数据，已脱敏）
        policy_rules: dict | None，策略评估快照（checked/fired/reason/policy_context）
        approval_outcome: str | None，人工审批结果（ApprovalOutcome 的取值）
        causation_id: str | None，因果回溯 span ID（decided 指向对应的 requested）
        request_model: str | None，请求的模型名（仅 llm_call / agent_started）
        response_model: str | None，端点实际报告的模型快照（仅 llm_call）
        provider: str | None，模型端点 host（仅 llm_call / agent_started）
        input_tokens: int | None，提示词 token 数（仅 llm_call）
        output_tokens: int | None，生成 token 数（仅 llm_call）
        finish_reason: str | None，结束原因（stop/length 等，仅 llm_call）
        task: str | None，agent 起跑时的任务文本（仅 agent_started；supervisor=用户原话）
        agent_version: str | None，agent 配置内容哈希前 16 位（仅 agent_started）
    """
    #: 事件类型标识。可选值："tool_started"、"tool_ended"、"approval_requested"、
    #: "approval_decided"、"agent_started"、"llm_call"。决定哪些其他字段有效。
    event_type: str
    #: 当前审计跨度（span）的唯一标识符，16 位十六进制字符（W3C 64-bit），
    #: 用于构建调用树。每个工具调用或审批/LLM/agent 事件都有独立的 span_id。
    span_id: str
    #: 一次完整 Agent.run() 执行的唯一追踪标识，32 位十六进制字符（W3C 128-bit），
    #: 用于聚合同一次任务中的所有事件（含嵌套调用）。
    trace_id: str
    #: 会话标识，跨多次运行保持一致，用于按对话线程分组审计日志。
    session_id: str
    #: 发起调用的 Agent 类型（如 "supervisor"、"paper-agent"）。
    agent_type: str
    #: 被调用的工具名称。非工具事件该字段为空字符串。
    tool_name: str
    #: 工具的风险档位（来源于 Tool.risk_level），未知工具或非工具事件为 "unknown"/""。
    risk_level: str
    #: 脱敏后的工具调用参数。凭证类键（api_key、token 等）被替换为 "***"，
    #: 路径类键（如 path）保留原值，其余原样——内容不再隐藏（取证需要原文）。
    input: Any = None
    #: 父跨度 ID，用于构建调用树。若为 None 则表示当前跨度是根节点。
    parent_span_id: str | None = None
    #: ReAct 循环轮次索引，从 0 开始计数。用于定位该事件发生在第几轮推理中。
    turn: int = 0
    #: 事件开始时间的 ISO 格式时间戳。
    started_at: str | None = None
    #: 事件结束时间的 ISO 格式时间戳。
    ended_at: str | None = None
    #: 策略引擎的最终决策。由 _derive_decision 推导，取值为
    #: `ToolDecision`（auto_allowed / user_confirmed / policy_denied /
    #: security_blocked / user_denied / error）。
    policy_decision: str = ""
    #: 工具执行的结果状态。由 _result_status 推导，取值为
    #: `ToolResultStatus`（success / policy_blocked / security_blocked /
    #: user_denied / error）。
    result_status: str = ""
    #: 工具执行耗时，单位为毫秒。由 started_at 和 ended_at 计算得出。
    duration_ms: int = 0
    #: 安全扫描违规明细。仅在 SecurityBlocked 场景下有值，
    #: 结构为 {"violations": [{"rule_id": ..., "severity": ..., "snippet": ...}, ...]}。
    security_scan: dict | None = None
    #: 格式化的错误信息（类型名 + 消息，截断至 200 字符）。
    #: 若无错误则为 None。供运维直接定位问题（如超时、404、IO 错误）。
    error: str | None = None
    #: 工具结果的**全量原文**（result.text，不截断、不压平换行）。
    #: 事后复现「这段输出是照着什么生成的」的一手材料。无结果时 None。
    output: Any = None
    #: 结构化摘要通道（ToolResult.summary，已脱敏）：spawn 的 digest、RAG 的
    #: documents 等结构化数据走这里——它们是全文之外的强类型视图。
    summary: Any = None
    #: 策略评估的快照。包含 checked（检查项列表）、fired（命中的规则名）、
    #: reason（拒绝/拦截原因）、policy_context（当时策略配置输入）。
    #: 用于事后 replay 当时决策依据。
    policy_rules: dict | None = None
    #: 人工审批结果。仅在 approval_decided 或 tool_ended 场景下有效，
    #: 取值为 `ApprovalOutcome`（user_confirmed / user_denied / auto_denied / cancelled）。
    approval_outcome: str | None = None
    #: 用于跨事件回溯的跨度 ID。在 approval_decided 事件中指向对应的
    #: approval_requested 事件，实现"请求 ≠ 决策"的因果关联。
    causation_id: str | None = None

    # ---------- 以下字段仅对 llm_call / agent_started 事件有效，其他事件类型为空 ----------
    #: 请求的模型名称（配置里的模型串）。仅 llm_call / agent_started 事件使用。
    request_model: str | None = None
    #: 端点在响应里自报的模型快照（OpenAI 兼容端点通常带版本后缀，比配置串精确）。
    #: 仅 llm_call 事件使用；端点不报时 None。
    response_model: str | None = None
    #: 模型端点 host（base_url 的网络位置）。仅 llm_call / agent_started 事件使用。
    provider: str | None = None
    #: 提示词消耗的 Token 数量（仅 llm_call 事件使用）。
    input_tokens: int | None = None
    #: 生成回复消耗的 Token 数量（仅 llm_call 事件使用）。
    output_tokens: int | None = None
    #: LLM 的结束原因，如 "stop"（正常结束）、"length"（超过 max_tokens 截断）等（仅 llm_call）。
    finish_reason: str | None = None
    #: agent 起跑时的任务文本。仅 agent_started 事件使用；
    #: supervisor 是用户原话，子 agent 是 supervisor 拼装的子任务文本。
    task: str | None = None
    #: agent 配置的内容哈希（sha256 前 16 hex），输入 = AGENT.md 正文 + 工具名清单 +
    #: 各工具的安全元数据。仅 agent_started 事件使用——判断「行为异常是不是配置刚变过」。
    agent_version: str | None = None

    #: dataclass 属性名 → 落盘 JSON 键的映射。需要换成业界标准词（带点号的
    #: OTel 属性名）的字段在此登记，其余键同名落盘。
    _JSON_KEY_MAP: ClassVar[dict[str, str]] = {
        "request_model": "gen_ai.request.model",
        "response_model": "gen_ai.response.model",
        "provider": "gen_ai.provider.name",
        "input_tokens": "gen_ai.usage.input_tokens",
        "output_tokens": "gen_ai.usage.output_tokens",
    }

    def to_json_dict(self) -> dict:
        """序列化为落盘用 dict：属性名映射为标准 JSON 键，全部字段恒在。

        每行键集固定（与事件类型无关）是刻意的：消费方按固定 schema 读行，
        不用对每个键做「可能不存在」防御；跨事件类型用不到的字段以 null 落盘。

        Returns:
            dict，键为标准审计字段名（含 gen_ai.* 点号形式），值为该事件的字段值。
        """
        return {
            self._JSON_KEY_MAP.get(f, f): getattr(self, f)
            for f in self.__dataclass_fields__ if not f.startswith("_")
        }
