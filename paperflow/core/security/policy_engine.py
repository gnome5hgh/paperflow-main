# paperflow/core/security/policy_engine.py
"""
PolicyEngineMiddleware —— 三级策略检查中间件。

在 ``before`` 阶段按顺序执行三级检查：

1. ``blocked_by_default``：工具被标记为默认禁止，直接抛 ``PolicyDenied``；
2. 风险阈值：工具 ``risk_level`` 超过会话阈值 ``max_risk`` 即抛
   ``PolicyDenied``（未知风险等级按 critical 处理）；
3. ``requires_confirm``：需要确认且本会话尚未确认过的工具抛
   ``ConfirmRequired``，用户调用 ``confirm()`` 后同工具不再询问。
"""

from paperflow.core.security import (
    SecurityMiddleware, ToolContext, PolicyDenied, ConfirmRequired,
)
from paperflow.core.tool import RISK_ORDER


class PolicyEngineMiddleware(SecurityMiddleware):
    def __init__(self, max_risk: str = "medium"):
        if max_risk not in RISK_ORDER:
            raise ValueError(
                f"非法风险阈值: {max_risk}，合法值: {sorted(RISK_ORDER.keys())}"
            )
        self.max_risk = max_risk
        self._confirmed: set[str] = set()

    async def before(self, ctx: ToolContext) -> None:
        tool = ctx.tool

        if tool.blocked_by_default:
            raise PolicyDenied(
                reason=f"'{tool.name}' 被标记为默认禁止，需手动覆盖才可执行"
            )

        tool_risk = RISK_ORDER.get(tool.risk_level, 3)  # 未知 → critical
        threshold = RISK_ORDER[self.max_risk]
        if tool_risk > threshold:
            raise PolicyDenied(
                reason=f"风险等级 {tool.risk_level} 超过会话阈值 {self.max_risk}"
            )

        if tool.requires_confirm and tool.name not in self._confirmed:
            raise ConfirmRequired(
                tool_name=tool.name,
                params=ctx.args,
                risk_level=tool.risk_level,
                side_effects=tool.side_effects,
                on_confirmed=lambda: self._confirmed.add(tool.name),
            )
