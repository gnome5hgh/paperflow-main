# paperflow/core/security/audit.py
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

from paperflow.core.security import (
    SecurityMiddleware, ToolContext, PolicyDenied, SecurityBlocked, ConfirmRequired,
)

#: 敏感键名 → 脱敏替换值 的模式表，按顺序匹配，命中即替换
SENSITIVE_KEY_PATTERNS = [
    (r"api_key|token|password|secret", "***"),
    (r"content|text|abstract|full_text|body", "HIDDEN"),
]
#: 路径类键名集合：命中则保留原值（安全：仅暴露文件名/路径，不泄露文件内容）
PATH_KEYS = frozenset({"path", "pdf_path", "file_path", "note_path", "source"})

#: 审计 span 栈：当前工具调用上下文。值 = {"span_id": str, "depth": int} 或 None。
#: 父链传播机制（对齐 OTel/LangSmith 的 contextvar 方案）——AuditMiddleware.before
#: 压栈，after 弹栈；子 agent（spawn 工具在 to_thread + asyncio.run 内执行）自动继承。
_span_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("audit_span", default=None)


@dataclass
class AuditEntry:
    event_type: str
    span_id: str
    trace_id: str
    session_id: str
    agent_type: str
    tool_name: str
    risk_level: str
    params: dict
    parent_id: str | None = None
    depth: int = 0
    turn: int = 0
    started_at: str | None = None
    ended_at: str | None = None
    policy_decision: str = ""
    result_status: str = ""
    duration_ms: int = 0
    security_scan: dict | None = None
    error: str | None = None
    result_summary: dict | str | None = None
    policy_rules: dict | None = None
    approval_outcome: str | None = None
    causation_id: str | None = None
    # llm_call 专属
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None


def _sanitize(args: dict) -> dict:
    """按敏感键名模式表脱敏调用参数：密钥/内容类替换，路径类保留原值。"""
    # 防御：大模型可能返回非 dict 的 JSON（如数组/字符串），脱敏不应崩溃
    if not isinstance(args, dict):
        return {}
    sanitized = {}
    for key, value in args.items():
        for pattern, replacement in SENSITIVE_KEY_PATTERNS:
            if re.search(pattern, key, re.IGNORECASE):
                sanitized[key] = replacement
                break
        else:
            if key in PATH_KEYS and isinstance(value, str):
                sanitized[key] = value
            else:
                sanitized[key] = value
    return sanitized


def _derive_decision(ctx: ToolContext) -> str:
    """从调用结果推导策略决策值：无异常时按是否用户确认区分，异常时按其类型区分。"""
    if ctx.error is None:
        return "user_confirmed" if ctx.user_confirmed else "auto_allowed"
    if isinstance(ctx.error, ConfirmRequired):
        return "user_denied"
    # 工具抛出的普通异常（RuntimeError 等）没有 decision 属性
    return getattr(ctx.error, "decision", "error")


def _result_status(ctx: ToolContext) -> str:
    """从异常类型推导结果状态：无异常为成功，否则按异常种类归为被拦截或出错。"""
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
    """安全拦截时取出违规明细写入审计；非拦截场景返回 None。"""
    if isinstance(ctx.error, (SecurityBlocked,)):
        return {"violations": ctx.error.violations}
    return None


def _format_error(e: Exception) -> str:
    """错误详情：类型名 + 消息，截断 200 字符，供运维直接定位（超时/404/IO）。"""
    s = f"{type(e).__name__}: {e}"
    return s[:200]


def _derive_fired(ctx: ToolContext) -> str | None:
    """命中规则：策略引擎显式标注优先；否则按异常类型兜底推断。"""
    if ctx.policy_fired:
        return ctx.policy_fired
    if isinstance(ctx.error, SecurityBlocked):
        return "security_scan"
    return None


def _build_policy_rules(ctx: ToolContext) -> dict | None:
    """组装策略依据快照：记录「当前配置输入」而非版本号，便于 replay 当时决策。"""
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
    """结果副作用摘要：优先 summary dict（脱敏），否则取 text 截断 200 字符。"""
    if result is None:
        return None
    if getattr(result, "summary", None):
        return _sanitize(dict(result.summary))
    text = getattr(result, "text", "") or ""
    return text.replace("\n", " ")[:200] or None


class AuditMiddleware(SecurityMiddleware):
    """审计中间件：把工具调用与审批/LLM 事件追加写入当日 JSONL 文件。"""

    def __init__(self, audit_dir: str = "data/audit"):
        self.audit_dir = audit_dir
        # 并发写锁：多个子代理的工具调用可能在不同线程并发写入，JSONL 追加写
        # 不加锁会出现行与行互相穿插、污染审计记录；加锁保证逐行原子写入。
        self._lock = threading.Lock()

    def _current_path(self) -> Path:
        # 跨日滚动：每次写盘按当天文件名解析（修复长驻进程跨零点写错文件）
        return Path(self.audit_dir) / f"audit_{datetime.now():%Y%m%d}.jsonl"

    def _write_event(self, entry: AuditEntry) -> None:
        # 跨日滚动：mkdir 与 open 共用同一次路径解析，避免跨零点时两者解析出不同文件名
        path = self._current_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 加锁写盘：整段「open + write」在锁内，保证 JSONL 行不会被并发线程分段交叉
            with self._lock:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
        except Exception as e:
            # 写盘失败（磁盘满/权限）不得中断工具结果返回：打印告警后降级跳过，
            # 与 _run_after_hooks 的哲学一致——审计是横切关注点，失败不该毁掉主流程
            print(f"[audit] write failed: {e}", file=sys.stderr)

    async def before(self, ctx: ToolContext) -> None:
        # 生成当前 span_id，读栈顶作父链；先写 tool_started 再压 contextvar——
        # 保证任何子事件（spawn 的子 agent 跑在 execute 内）落盘前，父 span 的
        # 起始事件已存在，中断也不会把子树写成孤儿。
        span = _span_ctx.get()
        span_id = f"span_{uuid.uuid4().hex[:12]}"
        ctx.span_id = span_id
        ctx.parent_id = span["span_id"] if span else None
        ctx.depth = (span["depth"] + 1) if span else 0
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
        ctx._audit_token = _span_ctx.set({"span_id": span_id, "depth": ctx.depth})

    async def after(self, ctx: ToolContext) -> None:
        # 防御：early-return 路径（JSON 解析失败/未知工具）只走 after，before 未执行
        # → ctx.span_id 为 None，则此处补一个 span 并补写 tool_started（树不变量：
        # 每个 span 必有起始事件）；未压栈则一律不弹栈。
        if ctx.span_id is None:
            ctx.span_id = f"span_{uuid.uuid4().hex[:12]}"
            # 防御路径仍要把自己挂到当前调用链下：嵌套工具（子 agent 幻觉未知工具/坏
            # JSON）命中此分支时栈顶是外层 span，不读则父链丢失、被误写成根节点。
            span = _span_ctx.get()
            if span:
                ctx.parent_id = span["span_id"]
                ctx.depth = span["depth"] + 1
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
            _span_ctx.reset(getattr(ctx, "_audit_token", None))
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
        # 审批生命周期：requested（发起确认时）与 decided（决策后）是两条独立事件，
        # decided 通过 causation_id 回溯 requested（合规要求：请求≠决策）。
        # 守卫：phase 只允许两值，拦截笔误（如 "reuested"）写入日志，避免污染审计。
        if phase not in {"requested", "decided"}:
            raise ValueError(f"invalid approval phase: {phase}")
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
        # requested 的 span 记到 ctx 上，供 decided 回溯；decided 则把自身 span 与
        # 最终结果记到 ctx 上，供 after 写 tool_ended 时带上因果链与审批结果。
        if phase == "requested":
            ctx._approval_requested_span_id = entry.span_id
        elif phase == "decided":
            ctx.approval_decided_span_id = entry.span_id
            ctx.approval_outcome = approval_outcome
        self._write_event(entry)

    def record_llm_call(self, *, trace_id, session_id, agent_type, turn, model,
                        prompt_tokens, completion_tokens, total_tokens,
                        started_at, duration_ms, finish_reason) -> None:
        # 同步（LLM 流式回调可能跑在线程池线程）：元数据 only，不记 content。
        # ended_at 由 started_at + duration_ms 推算，与调用方记录的起点一致；
        # started_at 格式无法解析时兜底为写入时刻。
        ended_at = datetime.now().isoformat()
        if started_at:
            try:
                ended_at = (datetime.fromisoformat(started_at)
                            + timedelta(milliseconds=duration_ms)).isoformat()
            except ValueError:
                pass
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
