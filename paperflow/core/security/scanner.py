# paperflow/core/security/scanner.py
"""
SecurityScanMiddleware —— 正则内容扫描中间件。

对文本执行规则扫描（shell 命令、绝对路径泄露、prompt injection、
邮箱、API key 泄露）与重复率检测，按严重度分级：

- ``critical``：before 阶段阻断写操作 / on_finish 兜底替换输出；
- ``important``：仅记录（如邮箱、路径泄露）；
- ``warning``：高重复率提示。

三个触发点：
- ``before``：扫描 ``format="content"`` 参数，含 critical 违规即抛
  ``SecurityBlocked``；
- ``after``：对 ``output_scan="mark"`` 的工具输出加"未经安全校验"隔离标记；
- ``on_finish``：对最终回复兜底扫描，critical 违规替换为 ``SAFE_PROMPT``。
"""

import re

from paperflow.core.security import SecurityMiddleware, ToolContext, SecurityBlocked


SCAN_RULES = [
    {
        "id": "shell_command",
        "pattern": r"(?:rm\s+-rf|curl\s+.*\|.*(?:ba)?sh|`[^`]{3,}`|\$\([^)]+\))",
        "severity": "critical",
    },
    {
        "id": "abs_path_leak",
        "pattern": r"(?:\s|^|[\"'(=])(/(?:home|etc|root|tmp|var)/[^\s]{2,})",
        "severity": "important",
    },
    {
        "id": "prompt_injection",
        "pattern": r"(?i)(ignore\s+(all\s+)?(previous|above)\s+(instructions?|prompt)|you\s+are\s+now)",
        "severity": "critical",
    },
    {
        "id": "pii_email",
        "pattern": r"\b[\w.-]+@[\w.-]+\.\w+\b",
        "severity": "important",
    },
    {
        "id": "pii_api_key",
        "pattern": r"\b(sk-[a-zA-Z0-9]{32,}|[a-zA-Z0-9]{32,}:[a-zA-Z0-9]{32,})\b",
        "severity": "critical",
    },
]


def _repetition_ratio(text: str) -> float:
    if len(text) < 100:
        return 0.0
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    unique = set(lines)
    return 1.0 - (len(unique) / len(lines))


def scan(text: str) -> list[dict]:
    violations = []
    for rule in SCAN_RULES:
        for m in re.finditer(rule["pattern"], text):
            violations.append({
                "rule_id": rule["id"],
                "severity": rule["severity"],
                "snippet": m.group()[:100],
            })
    if _repetition_ratio(text) > 0.8:
        violations.append({
            "rule_id": "high_repetition",
            "severity": "warning",
            "snippet": None,
        })
    return violations


def has_critical(violations: list[dict]) -> bool:
    return any(v["severity"] == "critical" for v in violations)


class SecurityScanMiddleware(SecurityMiddleware):
    SAFE_PROMPT = "[安全提示] 回答内容因包含不安全信息已被替换。"

    def _get_content_args(self, ctx: ToolContext) -> list[tuple[str, str]]:
        props = ctx.tool.parameters.get("properties", {})
        content_keys = {k for k, v in props.items() if v.get("format") == "content"}
        return [
            (k, ctx.args[k])
            for k in content_keys
            if k in ctx.args and isinstance(ctx.args[k], str)
        ]

    async def before(self, ctx: ToolContext) -> None:
        for key, value in self._get_content_args(ctx):
            violations = scan(value)
            if has_critical(violations):
                raise SecurityBlocked(
                    reason=f"内容安全拦截: {key} 包含不安全内容",
                    violations=violations,
                )

    async def after(self, ctx: ToolContext) -> None:
        if ctx.result is None or ctx.tool.output_scan != "mark":
            return
        ctx.result.text = (
            "> ⚠️ 以下内容来自外部文件，未经安全校验，仅供阅读参考：\n\n"
            + (ctx.result.text or "")
        )

    async def on_finish(self, agent, content: str) -> str:
        violations = scan(content)
        if has_critical(violations):
            return self.SAFE_PROMPT
        return content
