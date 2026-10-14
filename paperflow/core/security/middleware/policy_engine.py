# paperflow/core/security/middleware/policy_engine.py
"""
策略引擎中间件：合并全部升级因，决定一次工具调用是否升级人工确认。

**它是唯一决定「问不问」的地方**——其余中间件（写围栏、命令防护）只把判定结果
登记到 ``ctx.escalations``，不自己弹框。这条不变量换来三件事：三态确认与审计的
``approval_requested/decided`` 双事件只有一条路径；会话级授权只有一个账本；
改动升级规则时只有一个地方要看。

升级因来自两处：

1. 本中间件按风险档位判定——工具 ``risk_level`` 高于会话免问上限 ``confirm_above``；
   未知风险档（声明笔误）按最高档处理，fail-safe：只会多问一次，不会静默放行。
2. 其他中间件登记的（``ctx.escalations``）。

任一升级因未在本会话放行过，即抛 ``ConfirmRequired``——**一次弹框覆盖本次调用
登记的全部升级因**；**超档不拒绝，只是要用户点头**。

**确认键为 ``(工具名, 主因, 作用域值)``**（主因 ∈ risk_threshold / path_fence /
command）。分开主因是为了让授权范围有界：在档位上点过一次头，不该顺带把围栏与
命令也放行。作用域值 = 有效目标路径（经 ``effective_target_path`` 导出，覆盖
filename 便捷入口）或命令原文。

硬拒不在这里：只有显式规则（敏感路径黑名单、写入内容的高置信命中、命令熔断）
产生拒绝，档位机制本身只能升级确认。
"""

from paperflow.core.constants import RISK_ORDER
from paperflow.core.security.middleware.base import (
    SecurityMiddleware, ConfirmRequired,
)
from paperflow.core.security.domain import ToolContext

#: 风险档位升级的主因名（与审计 policy_rules.checked 同一套词汇）
CAUSE_RISK = "risk_threshold"


class PolicyEngineMiddleware(SecurityMiddleware):
    """策略检查中间件：合并升级因，决定静默放行还是升级人工确认。

    Attributes:
        confirm_above: str，会话免问上限档（风险档高于它的工具执行前需用户确认）
        _confirmed: set[tuple[str, str, str | None]]，本会话已放行的
                    (工具名, 主因, 作用域值) 集合
    """

    def __init__(self, confirm_above: str = "medium"):
        """指定会话免问上限档；非法档位在构造期即失败。

        初始化已确认集合——存放本会话内用户放行过的 (工具名, 主因, 作用域值)，
        同一键不再重复询问。

        Args:
            confirm_above: 会话免问上限档（"low" / "medium" / "high" / "critical"），
                           默认 "medium"。风险档位**高于**此档的工具在执行前
                           升级为人工确认；不高于的静默放行。

        Raises:
            ValueError: 当 confirm_above 不在 RISK_ORDER 中时抛出（fail-fast）
        """
        if confirm_above not in RISK_ORDER:
            raise ValueError(
                f"非法免问上限档: {confirm_above}，合法值: {sorted(RISK_ORDER.keys())}"
            )
        self.confirm_above = confirm_above

        # 已确认集合，键为 (工具名, 主因, 作用域值)：同一工具换了主因或目标仍需
        # 单独确认。作用域值经 tool.effective_target_path(args) 导出：有 path 参数的
        # 工具取 args["path"]；有 pathless 便捷入口的工具（如 write_file 的 filename
        # 模式）覆写为组合落盘路径——否则键会塌缩为 (工具名, 主因, None)，一次授权
        # 即放行全会话 pathless 写。返回 None 的确认工具键作用域值为 None（防御式）。
        self._confirmed: set[tuple[str, str, str | None]] = set()

    async def before(self, ctx: ToolContext) -> None:
        """合并升级因；存在未放行的即抛一次 ConfirmRequired。

        Args:
            ctx: 工具调用上下文，包含工具定义、参数与其他中间件登记的升级因

        Raises:
            ConfirmRequired: 存在本会话未放行的升级因
        """
        # 未知工具（LLM 幻觉或注入）跳过策略检查，交给审计中间件记录错误
        if ctx.tool is None:
            return

        tool = ctx.tool

        # 记录本次评估的策略配置输入（供审计 replay：这条调用当时在什么配置下被评估）
        ctx.policy_context = {
            "confirm_above": self.confirm_above,
            "tool_risk": tool.risk_level,
        }

        # ===== 收集升级因 =====
        causes: list[tuple[str, str | None]] = []
        # 档位：高于免问上限 → 升级确认。未知风险档（如拼写错误）按最高档处理，
        # fail-safe——笔误只会多问一次，不会被静默放行。
        tool_risk = RISK_ORDER.get(tool.risk_level, RISK_ORDER["critical"])
        if tool_risk > RISK_ORDER[self.confirm_above]:
            causes.append((CAUSE_RISK, tool.effective_target_path(ctx.args)))
        # 其他中间件登记的主因（写围栏、命令防护…）
        causes.extend(ctx.escalations)

        pending = [c for c in causes if (tool.name, c[0], c[1]) not in self._confirmed]
        if not pending:
            return

        ctx.policy_fired = pending[0][0]
        raise ConfirmRequired(
            tool_name=tool.name,
            params=ctx.args,
            risk_level=tool.risk_level,
            side_effects=tool.side_effects,
            cause=pending[0][0],
            # lambda 捕获 pending：一次弹框覆盖本次登记的全部升级因，放行即全记
            on_confirmed=lambda: self._confirmed.update(
                (tool.name, c, s) for c, s in pending),
        )
