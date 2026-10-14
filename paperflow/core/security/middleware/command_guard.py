# paperflow/core/security/middleware/command_guard.py
"""
命令防护中间件：对声明为命令的参数做解析与定档。

**这是一条为将来准备的缝，今天零消费方**：只要有工具在参数 schema 里写
``"format": "shell_command"``，它的取值就会被这里接管；没有任何工具这么声明时，
本中间件对每次调用都是纯 no-op。于是「加一个能执行 shell 的工具」这件事不需要再
回来改安全模块。

处置两档：

- **熔断**（毁灭性指令）→ 抛 ``SecurityBlocked`` 硬拒；
- **未知 / 不在安全清单内** → 登记 ``command`` 升级因，交给 PolicyEngine 统一弹一次确认。

判定细节见 ``services/command.py``（为什么用语法树而不是正则，那里写清了）。
**它是策略不是隔离**——本项目没有隔离轴，所以这张表是命令与宿主之间唯一的东西。
"""

from paperflow.core.security.middleware.base import SecurityMiddleware, SecurityBlocked
from paperflow.core.security.domain import ToolContext
from paperflow.core.security.services.command import analyze


class CommandGuardMiddleware(SecurityMiddleware):
    """命令防护：解析 ``format="shell_command"`` 参数并逐条定档。"""

    def _command_params(self, ctx: ToolContext) -> list[str]:
        """取出工具入参里所有声明为 shell_command 格式的字符串参数值。

        Args:
            ctx: 工具调用上下文

        Returns:
            list[str]：命令文本值（仅字符串且非空白）。
        """
        props = ctx.tool.parameters.get("properties", {})
        names = [k for k, v in props.items() if v.get("format") == "shell_command"]
        return [ctx.args[k] for k in names
                if isinstance(ctx.args.get(k), str) and ctx.args[k].strip()]

    async def before(self, ctx: ToolContext) -> None:
        """解析命令参数：熔断即硬拒，未知/不在清单内即登记升级因。

        Args:
            ctx: 工具调用上下文

        Raises:
            SecurityBlocked: 命令命中熔断表（提权、格式化、删根、写设备节点、fork 炸弹）
        """
        if ctx.tool is None:
            return

        for raw in self._command_params(ctx):
            analysis = analyze(raw)
            if analysis.circuit_break:
                raise SecurityBlocked(
                    reason=f"危险命令已熔断: {'；'.join(analysis.circuit_break)}",
                    violations=[{
                        "rule_id": "command_circuit_break",
                        "severity": "critical",
                        "snippet": raw[:100],
                    }],
                )
            if analysis.needs_escalation:
                # 主因与作用域值 = 命令原文：同一工具的不同命令各自确认
                ctx.escalations.append(("command", raw))
