# paperflow/core/security/middleware/audit.py
"""
审计中间件：把所有可追溯事件写进当天的 JSONL 日志文件。

记录五种事件，统一追加写入 ``audit_YYYYMMDD.jsonl``：

- ``tool_started``：工具调用开始（span 建立）——携带工具名/风险等级/入参/
  开始时间。在任何子事件写盘前先落盘，保证父链上每个 span 都有起始事件，
  中断时不会产生孤儿子树；
- ``tool_ended``：工具调用结束（span 收口）——同 span 携带决策、结果状态、
  耗时、错误详情、策略依据、审批结果与因果链。``tool_started`` 无配对
  ``tool_ended`` 即中断，可据此识别未完成的调用；
- ``approval_requested`` / ``approval_decided``：审批请求与决策是两条独立事件，
  decided 通过 ``causation_id`` 回溯到 requested（请求 ≠ 决策）；
- ``llm_call``：LLM 调用元数据（模型 / token 数 / 耗时），不记 content。

写入前的处理：
- 脱敏（``_sanitize``）：按敏感键名模式替换参数值（密钥类 → ``"***"``，
  内容类 → ``"HIDDEN"``），路径类键保留原值，便于追溯文件操作；
- 决策推导（``_derive_decision``）：根据调用结果推导出 auto_allowed /
  user_confirmed / policy_denied / security_blocked / user_denied；
- 状态推导（``_result_status``）：根据异常类型推导出 success /
  policy_blocked / security_blocked / user_denied / error。

调用链通过 contextvar 维护：``before`` 写 start 事件后压栈，``after`` 写 end
事件后弹栈；子 agent 在 to_thread + asyncio.run 里自动继承父链，各并发任务持
独立的 context 副本，互不串扰。每天一个文件、追加写入，写盘时按当天解析文件名，
长驻进程跨零点也能写对文件。等待用户确认（``ConfirmRequired``）的调用统一
记为 ``user_denied``——只有最终被确认放行（user_confirmed）的调用才视为
允许，未确认即未发生。
"""

import contextvars
import json
import re
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from paperflow.core.security.base import (
    SecurityMiddleware, ToolContext, PolicyDenied, SecurityBlocked, ConfirmRequired,
)
from paperflow.core.security.text import sanitize_surrogates

#: 敏感键名 → 脱敏替换值 的模式表，按顺序匹配，命中即替换
SENSITIVE_KEY_PATTERNS = [
    (r"api_key|token|password|secret", "***"),
    (r"content|text|abstract|full_text|body", "HIDDEN"),
]
#: 路径类键名集合：命中则保留原值（安全：仅暴露文件名/路径，不泄露文件内容）
PATH_KEYS = frozenset({"path", "pdf_path", "file_path", "note_path", "source"})

#: 审计 span 栈：当前工具调用上下文。值 = {"span_id": str, "depth": int} 或 None。
#: 用 contextvar 传播父链——before 压栈、after 弹栈；子 agent（spawn 工具在
#: to_thread + asyncio.run 内执行）自动继承父链，各并发任务持独立副本互不串扰。
_span_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("audit_span", default=None)


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
    #: 发起调用的 Agent 类型（如 "supervisor"、"searcher"）。
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


def _sanitize(args: dict) -> dict:
    """按敏感键名模式表脱敏调用参数：密钥/内容类替换，路径类保留原值。

    Args:
        args: dict，LLM 给出的原始调用参数（非 dict 时按空处理）

    Returns:
        脱敏后的参数字典（敏感键替换为占位，路径类键保留原值）。
    """
    # 防御：大模型可能返回非 dict 的 JSON（如数组/字符串），脱敏不应崩溃
    if not isinstance(args, dict):
        return {}

    sanitized = {}
    for key, value in args.items():
        # 顺序匹配敏感模式：命中则用对应替换值，break 跳出内层循环
        for pattern, replacement in SENSITIVE_KEY_PATTERNS:
            if re.search(pattern, key, re.IGNORECASE):
                sanitized[key] = replacement
                break
        else:
            # 未命中任何敏感模式：若键属于路径类且值为字符串则原样保留（便于追溯文件操作）
            if key in PATH_KEYS and isinstance(value, str):
                sanitized[key] = value
            else:
                sanitized[key] = value
    return sanitized


def _derive_decision(ctx: ToolContext) -> str:
    """从调用结果推导策略决策值：无异常按是否用户确认区分，异常按其类型区分。

    语义是「未确认即未发生」：抛 ConfirmRequired 的调用无论最终是否放行，
    只要走到这里（错误路径）一律记为 user_denied；只有通过确认分支、置了
    user_confirmed 并成功执行（无异常）的调用才记为 user_confirmed。

    Args:
        ctx: ToolContext，本次工具调用上下文

    Returns:
        策略决策字符串（未确认即未发生：异常路径一律记为 user_denied）。
    """
    if ctx.error is None:
        # 无异常：根据用户确认标志区分自动允许与用户确认
        return "user_confirmed" if ctx.user_confirmed else "auto_allowed"

    if isinstance(ctx.error, ConfirmRequired):
        # 即使 ConfirmRequired 最终被确认，但走到这里时 error 仍存在，
        # 说明是用户拒绝或超时未确认，一律视为 user_denied
        return "user_denied"

    # 其他异常（PolicyDenied / SecurityBlocked 或普通异常）取异常的 decision 属性，
    # 若无则兜底为 "error"（工具抛出的普通异常（RuntimeError 等）没有 decision 属性）
    return getattr(ctx.error, "decision", "error")


def _result_status(ctx: ToolContext) -> str:
    """从异常类型推导结果状态：无异常为成功，否则按异常种类归为被拦截或出错。

    Args:
        ctx: ToolContext，本次工具调用上下文

    Returns:
        结果状态字符串（success/policy_blocked/security_blocked/user_denied/error）。
    """
    if ctx.error is None:
        return "success"
    if isinstance(ctx.error, PolicyDenied):
        return "policy_blocked"
    if isinstance(ctx.error, SecurityBlocked):
        return "security_blocked"
    if isinstance(ctx.error, ConfirmRequired):
        return "user_denied"
    return "error"


def _extract_violations(ctx: ToolContext) -> dict | None:
    """安全拦截时取出违规明细写入审计；非拦截场景返回 None。

    Args:
        ctx: ToolContext，本次工具调用上下文

    Returns:
        违规明细 dict（仅 SecurityBlocked 场景）；其余情况 None。
    """
    # 如果 ctx.error 是 SecurityBlocked 类的实例，则执行后续操作
    if isinstance(ctx.error, (SecurityBlocked,)):
        return {"violations": ctx.error.violations}
    return None


def _format_error(e: Exception) -> str:
    """错误详情：类型名 + 消息，截断 200 字符，供运维直接定位（超时/404/IO）。

    Args:
        e: Exception，捕获到的异常

    Returns:
        「类型名: 消息」形式的文本，截断 200 字符。
    """
    s = f"{type(e).__name__}: {e}"
    return s[:200]


def _derive_fired(ctx: ToolContext) -> str | None:
    """命中规则：策略引擎显式标注优先；否则按异常类型兜底推断。

    Args:
        ctx: ToolContext，本次工具调用上下文

    Returns:
        命中的策略规则名；无命中返回 None。
    """
    if ctx.policy_fired:
        return ctx.policy_fired
    if isinstance(ctx.error, SecurityBlocked):
        return "security_scan"
    return None


def _build_policy_rules(ctx: ToolContext) -> dict | None:
    """组装策略依据快照：记录「当前配置输入」而非版本号，便于 replay 当时决策。

    Args:
        ctx: ToolContext，本次工具调用上下文

    Returns:
        策略依据快照 dict（checked/fired/reason/policy_context）；无策略信息时 None。
    """
    fired = _derive_fired(ctx)
    if ctx.policy_context is None and fired is None:
        return None
    return {
        "checked": ["blocked_by_default", "risk_threshold", "requires_confirm"],
        "fired": fired,
        "reason": str(ctx.error) if ctx.error else None,
        "policy_context": ctx.policy_context,
    }


def _result_summary(result) -> dict | str | None:
    """结果副作用摘要：优先 summary dict（脱敏），否则取 text 截断 200 字符。

    Args:
        result: ToolResult | None，工具执行结果

    Returns:
        副作用摘要：优先脱敏后的 summary dict，否则 text 去换行截断 200 字符；无结果 None。
    """
    if result is None:
        return None
    if getattr(result, "summary", None):
        return _sanitize(dict(result.summary))
    text = getattr(result, "text", "") or ""
    return text.replace("\n", " ")[:200] or None


class AuditMiddleware(SecurityMiddleware):
    """审计中间件：把工具调用与审批/LLM 事件追加写入当日 JSONL 文件。

    Attributes:
        audit_dir: str，审计日志目录（当日文件 audit_YYYYMMDD.jsonl）
        _lock: threading.Lock，JSONL 追加写锁（保证逐行原子写入）
    """

    def __init__(self, audit_dir: str = "data/security/audit"):
        """指定审计日志目录；默认落在工作区 data/security/audit 下。

        初始化时建好线程写锁——子 agent 的工具调用可能在不同线程并发写入，
        后面每次追加写都在锁内完成。

        Args:
            audit_dir: str，审计日志目录；默认 "data/security/audit"
        """
        self.audit_dir = audit_dir
        # 并发写锁：多个子代理的工具调用可能在不同线程并发写入，JSONL 追加写
        # 不加锁会出现行与行互相穿插、污染审计记录；加锁保证逐行原子写入。
        self._lock = threading.Lock()

    def _current_path(self) -> Path:
        """返回当日审计文件的绝对路径：每天一个 audit_YYYYMMDD.jsonl。"""
        # 跨日滚动：每次写盘按当天文件名解析（修复长驻进程跨零点写错文件）
        return Path(self.audit_dir) / f"audit_{datetime.now():%Y%m%d}.jsonl"

    def _write_event(self, entry: AuditEntry) -> None:
        """把一条事件追加写入当日 JSONL 文件；失败降级为 stderr 告警，不抛异常。

        Args:
            entry: AuditEntry，待落盘的审计事件

        Returns:
            无返回值；写盘失败降级为 stderr 告警，不抛异常。
        """
        # 跨日滚动：mkdir 与 open 共用同一次路径解析，避免跨零点时两者解析出不同文件名
        path = self._current_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 加锁写盘：整段「open + write」在锁内，保证 JSONL 行不会被并发线程分段交叉
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    # 写盘前清洗代理码点：入参/结果可能带 surrogate（surrogateescape
                    # 残留），严格 UTF-8 写文件会炸——清洗后再写，审计不丢行。
                    f.write(sanitize_surrogates(
                        json.dumps(entry.__dict__, ensure_ascii=False)) + "\n")
        except Exception as e:
            # 写盘失败（磁盘满/权限）不得中断工具结果返回：打印告警后降级跳过，
            # 与 _run_after_hooks 的哲学一致——审计是横切关注点，失败不该毁掉主流程
            print(f"[audit] write failed: {e}", file=sys.stderr)

    async def before(self, ctx: ToolContext) -> None:
        """工具执行前：为本次调用建立审计 span 并落盘 tool_started 起始事件。

        只观察不拦截，是管道里最先执行的一层——后续任何中间件拦截，调用已留痕。

        Args:
            ctx: ToolContext，本次工具调用上下文（写入 span 树字段）
        """
        # 1. 获取当前 contextvar 中存储的父 span（若有）
        # span 类似于 {"span_id": "span_xxx", "depth": 1}
        span = _span_ctx.get()

        # 2. 生成新的 span_id，更新当前工具调用的 ctx 中的树字段
        span_id = f"span_{uuid.uuid4().hex[:12]}"
        ctx.span_id = span_id
        ctx.parent_id = span["span_id"] if span else None
        ctx.depth = (span["depth"] + 1) if span else 0

        # 3. 立即落盘 tool_started 事件（在任何子事件发生之前）
        #    这是保证父链完整的关键：即使后续子工具写盘，父 start 已存在，不会出现孤儿
        self._write_event(AuditEntry(
            event_type="tool_started",
            span_id=span_id,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            agent_type=ctx.agent_type,
            tool_name=ctx.tool_name,
            risk_level=ctx.tool.risk_level if ctx.tool else "unknown",
            params=_sanitize(ctx.args),
            parent_id=ctx.parent_id,
            depth=ctx.depth,
            turn=ctx.turn,
            started_at=ctx.timestamp or "",
        ))

        # 4. 将当前 span 压入 contextvar 栈，供嵌套调用继承
        ctx._audit_token = _span_ctx.set({"span_id": span_id, "depth": ctx.depth})

    async def after(self, ctx: ToolContext) -> None:
        """工具执行后：收口当前 span 并落盘 tool_ended 收尾事件。

        before 正常执行过则弹栈；early-return 路径（JSON 解析失败 / 未知工具）
        只走 after，此处防御性补建 span 并补写 tool_started，保住「每个 span
        必有起始事件」的树不变量。

        Args:
            ctx: ToolContext，本次工具调用上下文（含结果或异常）
        """
        # ---- 防御性处理：若 before 未执行（如参数解析失败提前返回），则补建 span ----
        if ctx.span_id is None:
            # 补生成 span_id，并尝试从当前 contextvar 获取父链
            ctx.span_id = f"span_{uuid.uuid4().hex[:12]}"
            # 防御路径仍要把自己挂到当前调用链下：嵌套工具（子 agent 幻觉未知工具/坏
            # JSON）命中此分支时栈顶是外层 span，不读则父链丢失、被误写成根节点。
            span = _span_ctx.get()
            if span:
                ctx.parent_id = span["span_id"]
                ctx.depth = span["depth"] + 1
            # 补写 tool_started，保证树结构完整
            self._write_event(AuditEntry(
                event_type="tool_started",
                span_id=ctx.span_id,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                agent_type=ctx.agent_type,
                tool_name=ctx.tool_name,
                risk_level=ctx.tool.risk_level if ctx.tool else "unknown",
                params=_sanitize(ctx.args),
                parent_id=ctx.parent_id,
                depth=ctx.depth,
                turn=ctx.turn,
                started_at=ctx.timestamp or "",
            ))
        else:
            # before 已正常执行：从 contextvar 中弹出当前 span
            _span_ctx.reset(getattr(ctx, "_audit_token", None))

        # ---- 计算耗时并落盘 tool_ended ----
        duration = int((time.monotonic() - (ctx.started_at or 0.0)) * 1000)
        self._write_event(AuditEntry(
            event_type="tool_ended",
            span_id=ctx.span_id,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            agent_type=ctx.agent_type,
            tool_name=ctx.tool_name,
            risk_level=ctx.tool.risk_level if ctx.tool else "unknown",
            params=_sanitize(ctx.args),
            parent_id=ctx.parent_id,
            depth=ctx.depth,
            turn=ctx.turn,
            policy_decision=_derive_decision(ctx),
            result_status=_result_status(ctx),
            started_at=ctx.timestamp or "",
            ended_at=datetime.now().isoformat(),
            duration_ms=duration,
            security_scan=_extract_violations(ctx),
            error=_format_error(ctx.error) if ctx.error else None,
            result_summary=_result_summary(ctx.result),
            policy_rules=_build_policy_rules(ctx),
            approval_outcome=ctx.approval_outcome,
            causation_id=ctx.approval_decided_span_id,
        ))

    async def on_approval(self, ctx: ToolContext, phase: str, approval_outcome: str | None = None) -> None:
        """审批生命周期回调：requested 与 decided 各落盘一条独立审计事件。

        请求与决策是两条事件，decided 通过 causation_id 回溯到 requested
        （合规要求：请求 ≠ 决策）。

        Args:
            ctx: ToolContext，工具调用上下文
            phase: str，"requested" 或 "decided"
            approval_outcome: str | None，仅 decided 阶段有效（user_confirmed/user_denied/auto_denied）
        """
        # 审批生命周期：requested（发起确认时）与 decided（决策后）是两条独立事件，
        # decided 通过 causation_id 回溯 requested（合规要求：请求≠决策）。
        # 守卫：phase 只允许两值，拦截笔误（如 "reuested"）写入日志，避免污染审计。
        if phase not in {"requested", "decided"}:
            raise ValueError(f"invalid approval phase: {phase}")

        # 获取当前 span 上下文，决定本次审批事件的父链
        span = _span_ctx.get()
        now = datetime.now().isoformat()

        entry = AuditEntry(
            event_type=f"approval_{phase}",
            span_id=f"span_{uuid.uuid4().hex[:12]}",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            agent_type=ctx.agent_type,
            tool_name=ctx.tool_name,
            risk_level=ctx.tool.risk_level if ctx.tool else "unknown",
            params=_sanitize(ctx.args),
            parent_id=span["span_id"] if span else ctx.parent_id,
            depth=(span["depth"] + 1) if span else ctx.depth,
            turn=ctx.turn,
            started_at=now,
            ended_at=now,
            causation_id=getattr(ctx, "_approval_requested_span_id", None) if phase == "decided" else None,
            approval_outcome=approval_outcome if phase == "decided" else None,
        )

        # 根据阶段在 ctx 上存储必要的 span ID，供后续事件关联
        if phase == "requested":
            # 存储请求事件的 span_id，供 decided 事件通过 causation_id 回溯
            ctx._approval_requested_span_id = entry.span_id
        elif phase == "decided":
            # 存储决策事件的 span_id 和 outcome，供 tool_ended 事件携带
            ctx.approval_decided_span_id = entry.span_id
            ctx.approval_outcome = approval_outcome

        self._write_event(entry)

    def record_llm_call(self, *, trace_id, session_id, agent_type, turn, model,
                        prompt_tokens, completion_tokens, total_tokens,
                        started_at, duration_ms, finish_reason) -> None:
        """记录一次 LLM 调用元数据（模型 / token 数 / 耗时），不记 content。

        同步方法——LLM 流式回调可能跑在线程池线程，不能 await。

        Args:
            trace_id: str，本次 run 的追踪 ID
            session_id: str，会话标识
            agent_type: str，发起调用的 Agent 类型
            turn: int，ReAct 轮次
            model: str，模型名
            prompt_tokens: int，提示词 token 数
            completion_tokens: int，生成 token 数
            total_tokens: int，总 token 数
            started_at: str，调用开始时间（ISO）
            duration_ms: int，耗时（毫秒）
            finish_reason: str，结束原因（stop/length 等）
        """
        # 计算 ended_at：优先用 started_at + duration_ms 推算，否则兜底为当前时间
        ended_at = datetime.now().isoformat()
        if started_at:
            try:
                ended_at = (datetime.fromisoformat(started_at)
                            + timedelta(milliseconds=duration_ms)).isoformat()
            except ValueError:
                pass

        # 获取当前 span 上下文，将 LLM 调用作为子树挂到当前工具调用之下
        span = _span_ctx.get()

        entry = AuditEntry(
            event_type="llm_call",
            span_id=f"span_{uuid.uuid4().hex[:12]}",
            trace_id=trace_id, session_id=session_id, agent_type=agent_type,
            tool_name="", risk_level="", params={},
            parent_id=span["span_id"] if span else None,
            depth=(span["depth"] + 1) if span else 0,
            turn=turn,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            model=model, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, total_tokens=total_tokens,
            finish_reason=finish_reason,
        )
        self._write_event(entry)
