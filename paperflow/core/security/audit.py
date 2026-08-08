# paperflow/core/security/audit.py
"""
审计中间件：把每次工具调用记录成一条 JSONL 日志。

每次工具调用结束后（after 钩子），把本次调用的结果、决策与耗时追加写入
当天的日志文件 ``audit_YYYYMMDD.jsonl``，供事后追溯与合规检查使用。

写入前的处理：
- 脱敏（``_sanitize``）：按敏感键名模式替换参数值（密钥类 → ``"***"``，
  内容类 → ``"HIDDEN"``），路径类键保留原值，便于追溯文件操作；
- 决策推导（``_derive_decision``）：根据调用结果推导出 auto_allowed /
  user_confirmed / policy_denied / security_blocked / user_denied；
- 状态推导（``_result_status``）：根据异常类型推导出 success /
  policy_blocked / security_blocked / user_denied / error。

``before`` 钩子为空实现：审计只关心调用结束后的最终决策与结果。
每天一个文件、追加写入，天然按时间分片。

说明：等待用户确认（``ConfirmRequired``）的调用统一记为 ``user_denied``——
只有最终被确认放行（user_confirmed）的调用才视为允许，未确认即未发生。
"""

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
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


@dataclass
class AuditEntry:
    #: 单条审计记录 ID
    entry_id: str
    #: 归属的 Agent 运行 trace ID
    trace_id: str
    #: 归属的会话 ID
    session_id: str
    #: 工具调用发起时间（ISO 格式，来自 ToolContext）
    timestamp: str
    #: Agent 类型
    agent_type: str
    #: 工具名称
    tool_name: str
    #: 工具风险等级
    risk_level: str
    #: 脱敏后的调用参数
    params: dict
    #: 父审计记录 ID（预留给子工具调用链）
    parent_entry_id: str | None = None
    #: 调用链深度（预留给子工具调用链）
    depth: int = 0
    #: 策略决策推导结果（auto_allowed / user_confirmed / policy_denied /
    #: security_blocked / user_denied / error —— error 用于工具执行失败和
    #: 早期返回路径如 JSON 解析失败、未知工具）
    policy_decision: str = ""
    #: 结果状态推导结果
    result_status: str = ""
    #: 执行耗时（毫秒）
    duration_ms: int = 0
    #: 安全扫描违规详情（SecurityBlocked 时非空）
    security_scan: dict | None = None


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


class AuditMiddleware(SecurityMiddleware):
    """工具调用审计中间件：每次调用结束后追加写入一条 JSONL 审计记录。"""

    def __init__(self, audit_dir: str = "data/audit"):
        self.audit_path = Path(audit_dir) / f"audit_{datetime.now():%Y%m%d}.jsonl"
        # 并发写锁：多个子代理的工具调用可能在不同线程并发进入本中间件，
        # JSONL 追加写不加锁会出现行与行互相穿插、污染审计记录；加锁保证逐行原子写入。
        self._lock = threading.Lock()

    async def before(self, ctx: ToolContext) -> None:
        """工具执行前不做事：审计只关心调用结束后的最终决策与结果。"""
        return

    async def after(self, ctx: ToolContext) -> None:
        """把本次调用的完整审计记录追加写入当日日志文件。"""
        duration = int((time.monotonic() - (ctx.started_at or 0.0)) * 1000)
        entry = AuditEntry(
            entry_id=f"audit_{uuid.uuid4().hex[:8]}",
            timestamp=ctx.timestamp or "",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            agent_type=ctx.agent_type,
            tool_name=ctx.tool_name,
            risk_level=ctx.tool.risk_level if ctx.tool else "unknown",
            params=_sanitize(ctx.args),
            policy_decision=_derive_decision(ctx),
            result_status=_result_status(ctx),
            duration_ms=duration,
            security_scan=_extract_violations(ctx),
        )
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        # 加锁写盘：整段「open + write」在锁内，保证 JSONL 行不会被并发线程分段交叉
        with self._lock:
            with open(self.audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry.__dict__, ensure_ascii=False) + "\n")
