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
- ``agent_started``：agent 实例起跑——原始任务文本（supervisor=用户原话）/
  配置内容哈希 / 模型与端点，一次 run 一条；
- ``llm_call``：LLM 调用元数据（模型 / token 数 / 耗时），不记 prompt 正文。

写入前的处理：
- 脱敏（``_sanitize``）：只遮蔽凭证类键值（api_key/token/password/secret →
  ``"***"``），路径类键保留原值，**内容不再隐藏**——入参与返回都是全量原文，
  供事后取证回放（审计目录在防篡改黑名单内、且从不回灌模型，不构成注入面）；
- 决策推导（``_derive_decision``）：根据调用结果推导出 ``ToolDecision``
  （auto_allowed / user_confirmed / policy_denied / security_blocked / user_denied）；
- 状态推导（``_result_status``）：根据异常类型推导出 ``ToolResultStatus``
  （success / security_blocked / user_denied / error）。

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
from datetime import datetime, timedelta
from typing import Any
from pathlib import Path

from paperflow.core.security.constants.enums import ApprovalOutcome, ToolDecision, ToolResultStatus
from paperflow.core.security.middleware.base import (
    SecurityMiddleware, SecurityBlocked, ConfirmRequired,
)
from paperflow.core.security.domain import AuditEntry, ToolContext
from paperflow.core.common.text import sanitize_surrogates

#: 凭证类键名 → 替换值。这是审计里唯一的脱敏——凭证落日志是真泄露且对取证
#: 无价值；其余键值（含写入/读出的正文）全量原文保留。
CREDENTIAL_KEY_PATTERNS = [
    (r"api_key|token|password|secret", "***"),
]
#: 审计 span 栈：当前工具调用上下文。值 = {"span_id": str} 或 None。
#: 用 contextvar 传播父链——before 压栈、after 弹栈；子 agent（spawn 工具在
#: to_thread + asyncio.run 内执行）自动继承父链，各并发任务持独立副本互不串扰。
_span_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("audit_span", default=None)


def _sanitize(value):
    """递归脱敏：凭证类**键名**命中的值替换为占位，其余（含正文）原样保留。

    递归是必须的：工具入参与结构化 summary 都可能嵌套（如 MCP 工具的
    ``{"config": {"api_key": ...}}``、RAG 检索记录的 ``documents[].metadata``），
    只扫顶层会把嵌套凭证漏进日志。list 逐项走同一规则；字符串/标量原样返回。

    Args:
        value: 任意 JSON 形态的值（dict / list / str / 标量）

    Returns:
        脱敏后的同构值（凭证类键的值替换为 "***"，其余原样）。
    """
    if isinstance(value, dict):
        sanitized = {}
        for key, val in value.items():
            # 顺序匹配凭证模式：命中则替换，break 跳出内层循环
            for pattern, replacement in CREDENTIAL_KEY_PATTERNS:
                if re.search(pattern, key, re.IGNORECASE):
                    sanitized[key] = replacement
                    break
            else:
                sanitized[key] = _sanitize(val)
        return sanitized
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


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
        return ToolDecision.USER_CONFIRMED if ctx.user_confirmed else ToolDecision.AUTO_ALLOWED

    if isinstance(ctx.error, ConfirmRequired):
        # 即使 ConfirmRequired 最终被确认，但走到这里时 error 仍存在，
        # 说明是用户拒绝或超时未确认，一律视为 user_denied
        return ToolDecision.USER_DENIED

    # 其他异常（SecurityError 子类或普通异常）取异常的 decision 属性，
    # 若无则兜底为 error（工具抛出的普通异常（RuntimeError 等）没有 decision 属性）
    return getattr(ctx.error, "decision", ToolDecision.ERROR)


def _result_status(ctx: ToolContext) -> str:
    """从异常类型推导结果状态：无异常为成功，否则按异常种类归为被拦截或出错。

    Args:
        ctx: ToolContext，本次工具调用上下文

    Returns:
        结果状态字符串（success/security_blocked/user_denied/error）。
    """
    if ctx.error is None:
        return ToolResultStatus.SUCCESS
    if isinstance(ctx.error, SecurityBlocked):
        return ToolResultStatus.SECURITY_BLOCKED
    if isinstance(ctx.error, ConfirmRequired):
        return ToolResultStatus.USER_DENIED
    return ToolResultStatus.ERROR


def _extract_violations(ctx: ToolContext) -> dict | None:
    """安全拦截时取出违规明细写入审计；非拦截场景返回 None。

    Args:
        ctx: ToolContext，本次工具调用上下文

    Returns:
        违规明细 dict（仅 SecurityBlocked 场景）；其余情况 None。
    """
    findings = list(getattr(ctx, "content_findings", None) or [])
    if isinstance(ctx.error, SecurityBlocked):
        return {"violations": ctx.error.violations, "content_findings": findings}
    # 未被拦截但内容防护有结论（如语义判定命中）——同样入审计，便于事后回看
    if findings:
        return {"violations": [], "content_findings": findings}
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
        "checked": ["risk_threshold"],
        "fired": fired,
        "reason": str(ctx.error) if ctx.error else None,
        "policy_context": ctx.policy_context,
    }


def _result_fields(result) -> tuple[Any, Any]:
    """取结果的审计视图：``output`` 是全量原文（取证回放用），``summary`` 是结构化通道。

    Args:
        result: ToolResult | None，工具执行结果

    Returns:
        (output, summary) 二元组：output 为 result.text 全文（不截断、不压平）；
        summary 为脱敏后的 summary dict（spawn digest / RAG documents 等结构化
        数据的载体）。result 为 None 时两者皆 None。
    """
    if result is None:
        return None, None
    text = getattr(result, "text", "") or "" or None
    summary = getattr(result, "summary", None)
    summary = _sanitize(dict(summary)) if summary else None
    return text, summary


class AuditMiddleware(SecurityMiddleware):
    """审计中间件：把工具调用与审批/LLM 事件追加写入当日 JSONL 文件。

    Attributes:
        audit_dir: str，审计日志目录（当日文件 audit_YYYYMMDD.jsonl）
        _lock: threading.Lock，JSONL 追加写锁（保证逐行原子写入）
    """

    def __init__(self, audit_dir: str = ".paperflow/security/audit"):
        """指定审计日志目录；默认落在工作区 .paperflow/security/audit 下。

        初始化时建好线程写锁——子 agent 的工具调用可能在不同线程并发写入，
        后面每次追加写都在锁内完成。

        Args:
            audit_dir: str，审计日志目录；默认 ".paperflow/security/audit"
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
                        json.dumps(entry.to_json_dict(), ensure_ascii=False)) + "\n")
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
        # span 类似于 {"span_id": "16位hex"}
        span = _span_ctx.get()

        # 2. 生成新的 span_id，更新当前工具调用的 ctx 中的树字段
        span_id = uuid.uuid4().hex[:16]
        ctx.span_id = span_id
        ctx.parent_span_id = span["span_id"] if span else None

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
            input=_sanitize(ctx.args),
            parent_span_id=ctx.parent_span_id,
            turn=ctx.turn,
            started_at=ctx.timestamp or "",
        ))

        # 4. 将当前 span 压入 contextvar 栈，供嵌套调用继承
        ctx._audit_token = _span_ctx.set({"span_id": span_id})

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
            ctx.span_id = uuid.uuid4().hex[:16]
            # 防御路径仍要把自己挂到当前调用链下：嵌套工具（子 agent 幻觉未知工具/坏
            # JSON）命中此分支时栈顶是外层 span，不读则父链丢失、被误写成根节点。
            span = _span_ctx.get()
            if span:
                ctx.parent_span_id = span["span_id"]
            # 补写 tool_started，保证树结构完整
            self._write_event(AuditEntry(
                event_type="tool_started",
                span_id=ctx.span_id,
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                agent_type=ctx.agent_type,
                tool_name=ctx.tool_name,
                risk_level=ctx.tool.risk_level if ctx.tool else "unknown",
                input=_sanitize(ctx.args),
                parent_span_id=ctx.parent_span_id,
                turn=ctx.turn,
                started_at=ctx.timestamp or "",
            ))
        else:
            # before 已正常执行：从 contextvar 中弹出当前 span
            _span_ctx.reset(getattr(ctx, "_audit_token", None))

        # ---- 计算耗时并落盘 tool_ended ----
        duration = int((time.monotonic() - (ctx.started_at or 0.0)) * 1000)
        result_output, result_summary = _result_fields(ctx.result)
        self._write_event(AuditEntry(
            event_type="tool_ended",
            span_id=ctx.span_id,
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            agent_type=ctx.agent_type,
            tool_name=ctx.tool_name,
            risk_level=ctx.tool.risk_level if ctx.tool else "unknown",
            input=_sanitize(ctx.args),
            parent_span_id=ctx.parent_span_id,
            turn=ctx.turn,
            policy_decision=_derive_decision(ctx),
            result_status=_result_status(ctx),
            started_at=ctx.timestamp or "",
            ended_at=datetime.now().isoformat(),
            duration_ms=duration,
            security_scan=_extract_violations(ctx),
            error=_format_error(ctx.error) if ctx.error else None,
            output=result_output,
            summary=result_summary,
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
            approval_outcome: ApprovalOutcome | None，仅 decided 阶段有效
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
            span_id=uuid.uuid4().hex[:16],
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            agent_type=ctx.agent_type,
            tool_name=ctx.tool_name,
            risk_level=ctx.tool.risk_level if ctx.tool else "unknown",
            input=_sanitize(ctx.args),
            parent_span_id=span["span_id"] if span else ctx.parent_span_id,
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

    def record_agent_start(self, *, trace_id, session_id, agent_type, task,
                           agent_version=None, model=None, provider=None) -> None:
        """记录一次 agent 起跑（原始任务文本 + 配置版本 + 模型），同步方法。

        supervisor 的 task 是用户原话、子 agent 的 task 是子任务文本——这是
        「这条任务链在回答哪个问题」的取证锚点；agent_version 用来回答
        「行为异常是不是配置刚改过」。

        Args:
            trace_id: str，本次 run 的追踪 ID（子 agent 继承父的）
            session_id: str，会话标识
            agent_type: str，起跑的 Agent 类型
            task: str，本次 run 的任务文本（全量原文，不脱敏——它就是取证对象）
            agent_version: str | None，agent 配置内容哈希前 16 位
            model: str | None，模型名（配置串）
            provider: str | None，端点 host
        """
        # 起跑时若在某个工具调用内（spawn 的子 agent 正是如此），挂到该 span 下；
        # supervisor 顶层起跑时栈为空 → 根节点。
        span = _span_ctx.get()
        now = datetime.now().isoformat()
        entry = AuditEntry(
            event_type="agent_started",
            span_id=uuid.uuid4().hex[:16],
            trace_id=trace_id, session_id=session_id, agent_type=agent_type,
            tool_name="", risk_level="", input={},
            parent_span_id=span["span_id"] if span else None,
            turn=0,
            started_at=now,
            ended_at=now,
            task=task, agent_version=agent_version,
            request_model=model, provider=provider,
        )
        self._write_event(entry)

    def record_llm_call(self, *, trace_id, session_id, agent_type, turn, model,
                        input_tokens, output_tokens,
                        started_at, duration_ms, finish_reason,
                        response_model=None, provider=None) -> None:
        """记录一次 LLM 调用元数据（模型 / token 数 / 耗时），不记 prompt 正文。

        同步方法——LLM 流式回调可能跑在线程池线程，不能 await。

        Args:
            trace_id: str，本次 run 的追踪 ID
            session_id: str，会话标识
            agent_type: str，发起调用的 Agent 类型
            turn: int，ReAct 轮次
            model: str，模型名（配置串；端点自报快照另经 response_model）
            input_tokens: int，提示词 token 数
            output_tokens: int，生成 token 数
            response_model: str | None，端点自报的模型快照（不报则 None）
            provider: str | None，端点 host（不报则 None）
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
            span_id=uuid.uuid4().hex[:16],
            trace_id=trace_id, session_id=session_id, agent_type=agent_type,
            tool_name="", risk_level="", input={},
            parent_span_id=span["span_id"] if span else None,
            turn=turn,
            started_at=started_at,
            ended_at=ended_at,
            duration_ms=duration_ms,
            request_model=model, input_tokens=input_tokens,
            output_tokens=output_tokens,
            response_model=response_model, provider=provider,
            finish_reason=finish_reason,
        )
        self._write_event(entry)
