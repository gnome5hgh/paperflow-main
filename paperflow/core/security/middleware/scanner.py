# paperflow/core/security/scanner.py
"""
内容扫描中间件：对文本做正则规则扫描与重复率检测，按严重度分级处理。

内置规则包括：shell 命令、绝对路径泄露、提示注入、邮箱、API key 泄露，
外加高重复率检测。按严重度分级：

- 严重级：在工具执行前阻断写操作；最终回复兜底时替换为安全提示；
- 重要级：仅记录，不拦截（如邮箱、路径泄露）；
- 警告级：高重复率提示。

三个触发点：
- ``before``：扫描 ``format="content"`` 参数，含严重级违规即抛
  ``SecurityBlocked``；
- ``after``：对 ``output_scan="mark"`` 的工具输出加"未经安全校验"隔离标记；
- ``on_finish``：对最终回复兜底扫描，严重级违规替换为 ``SAFE_PROMPT``。
"""

import re

from paperflow.core.security.base import SecurityMiddleware, ToolContext, SecurityBlocked


#: 危险命令白名单：只有这些词在"命令位"出现才判严重级违规。
#: 采用白名单而非"任意命令形态 + 反引号元字符"的判定，是因为后者会把数学
#: 记号误判成 shell 命令：P(x|y)、f(x;θ)、$(x+y)$ 等都会命中 |/;/$() 的元字符
#: 形态。数学公式不以危险命令词开头，白名单天然豁免；大模型生成的恶意命令
#: 都是标准 shell 词，白名单可以覆盖且不丢真实威胁。cat/ls 保留，是为了维持
#: 既有测试对 "cat /etc/passwd、ls -la 仍命中" 的覆盖。
_DANGEROUS_COMMANDS = [
    # 破坏性/系统级
    "rm", "dd", "mkfs", "fdisk", "mount", "umount", "chmod", "chown",
    "kill", "pkill", "systemctl", "service", "docker", "podman", "crontab",
    # find：-exec/-delete 可执行/删除；测试要求 $(find / -name x) 必须命中
    "find",
    # 执行器
    "sh", "bash", "zsh", "python", "python3", "sudo", "eval", "exec", "tee",
    # 远程/数据外带
    # curl 刻意不在通用清单：curl 是向量微积分算子（∇×F），"计算 `curl F` 的
    # 点积"等数学公式会误判为 shell 命令。危险的下载后执行形态
    # （curl ... | sh）由 SHELL_COMMAND_RE 的专用分支 \bcurl\b...|...(ba)?sh\b
    # 兜住，不依赖通用清单。
    "wget", "nc", "ncat", "telnet", "ssh", "scp", "openssl", "base64",
    # 包管理（可装恶意软件）
    "apt", "apt-get", "yum", "dnf", "pip", "pip3", "npm", "nohup",
    # 保留既有测试要求的命令
    "cat", "ls",
]

#: 命令词 alternation（长词优先——apt-get 先于 apt，避免 \bapt\b 匹配 apt-get 前缀）
_CMDS = "|".join(sorted(_DANGEROUS_COMMANDS, key=len, reverse=True))

#: shell_command 规则（白名单）：
#: ① 反引号内：危险命令 + 空白参数 / ;|& 分隔 → `` `cat /etc/passwd` ``、`` `rm -rf /` ``
#: ② 裸 rm -rf（无反引号也拦）
#: ③ 裸 curl ... | sh
#: ④ $() 命令替换（要求危险命令词）：$(ls)、$(find / -name x)；$(x+y) 豁免
SHELL_COMMAND_RE = re.compile(
    rf"`[^`]*\b(?:{_CMDS})\b(?:\s+[^`\n]*|\s*[;|&])[^`]*`"
    rf"|\b(?:rm)\s+-rf"
    rf"|\b(?:curl)\b[^`\n;]*\|[^`\n]*(?:ba)?sh\b"
    rf"|\$\((?:\b(?:{_CMDS})\b(?:\s+[^)]*)?)\)"
)


SCAN_RULES = [
    {
        "id": "shell_command",
        "pattern": SHELL_COMMAND_RE.pattern,
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
    """计算文本重复率：按行去重后 1 - 唯一行数/总行数；文本过短或无有效行时返回 0。"""
    if len(text) < 100:
        return 0.0
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return 0.0
    unique = set(lines)
    return 1.0 - (len(unique) / len(lines))


def scan(text: str) -> list[dict]:
    """对文本执行全部规则扫描，返回违规列表；重复率过高时追加一条警告项。"""
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
    """判断违规列表中是否含严重级违规。"""
    return any(v["severity"] == "critical" for v in violations)


class SecurityScanMiddleware(SecurityMiddleware):
    """内容扫描中间件：在写入前拦截、在输出与最终回复上兜底处理不安全内容。"""

    SAFE_PROMPT = "[安全提示] 回答内容因包含不安全信息已被替换。"

    def _get_content_args(self, ctx: ToolContext) -> list[tuple[str, str]]:
        """从工具入参中取出所有声明为 content 格式的字符串参数。"""
        props = ctx.tool.parameters.get("properties", {})
        content_keys = {k for k, v in props.items() if v.get("format") == "content"}
        return [
            (k, ctx.args[k])
            for k in content_keys
            if k in ctx.args and isinstance(ctx.args[k], str)
        ]

    async def before(self, ctx: ToolContext) -> None:
        """扫描写入类工具的内容参数，含严重级违规即抛 SecurityBlocked 拦截。"""
        if ctx.tool is None:
            return
        for key, value in self._get_content_args(ctx):
            violations = scan(value)
            if has_critical(violations):
                raise SecurityBlocked(
                    reason=f"内容安全拦截: {key} 包含不安全内容",
                    violations=violations,
                )

    async def after(self, ctx: ToolContext) -> None:
        """对声明 output_scan="mark" 的工具输出加"未经安全校验"隔离标记。"""
        if ctx.result is None or ctx.tool.output_scan != "mark":
            return
        ctx.result.text = (
            "> ⚠️ 以下内容来自外部文件，未经安全校验，仅供阅读参考：\n\n"
            + (ctx.result.text or "")
        )

    async def on_finish(self, agent, content: str) -> str:
        """对最终回复兜底扫描：含严重级违规时整体替换为安全提示。"""
        violations = scan(content)
        if has_critical(violations):
            return self.SAFE_PROMPT
        return content
