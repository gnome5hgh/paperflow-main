# paperflow/core/security/policy_engine.py
"""
策略引擎中间件：在工具执行前做三级检查。

``before`` 阶段按顺序执行三级检查：

1. ``blocked_by_default``：工具被标记为默认禁止，直接抛 ``PolicyDenied``；
2. 风险阈值：工具 ``risk_level`` 超过会话阈值 ``max_risk`` 即抛
   ``PolicyDenied``（未知风险等级按最高级处理）；
3. ``requires_confirm``：需要确认且本会话尚未确认过的工具抛
   ``ConfirmRequired``，用户调用 ``confirm()`` 后同一（工具, 目标路径）
   不再重复询问。

确认按目标路径作用域：write_file/edit_file 是覆盖型写操作，若只按工具名
确认一次，后续任意路径都会被静默写入，可能误覆盖用户手写笔记，因此已确认
集合以（工具名, 目标路径）为键。
"""

from paperflow.core.security.base import (
    SecurityMiddleware, ToolContext, PolicyDenied, ConfirmRequired,
)
from paperflow.core.tool import RISK_ORDER


class PolicyEngineMiddleware(SecurityMiddleware):
    """策略检查中间件：默认禁止、风险阈值、确认放行三级检查。"""

    def __init__(self, max_risk: str = "medium"):
        """指定会话风险阈值；非法阈值在构造期即 fail-fast。

        初始化已确认集合——存放本会话内用户放行过的 (工具名, 目标路径)，
        同一键不再重复询问。
        """
        if max_risk not in RISK_ORDER:
            raise ValueError(
                f"非法风险阈值: {max_risk}，合法值: {sorted(RISK_ORDER.keys())}"
            )
        self.max_risk = max_risk
        # 已确认集合，键为 (工具名, 目标路径)：同一工具的不同路径仍需单独确认。
        # write_file/edit_file 都有 path 参数；没有 path 的确认工具键为 (name, None)，
        # 退化为旧的按工具名确认的行为（防御式，当前无此类工具）。
        self._confirmed: set[tuple[str, str | None]] = set()

    async def before(self, ctx: ToolContext) -> None:
        """按 默认禁止 → 风险阈值 → 确认放行 的顺序检查工具，违规即抛异常。"""
        if ctx.tool is None:
            return        # 未知工具交给 after 钩子做审计
        tool = ctx.tool
        # 记录本次评估的策略配置输入（供审计 replay：这条调用当时在什么配置下被评估）
        ctx.policy_context = {
            "max_risk": self.max_risk,
            "tool_risk": tool.risk_level,
            "blocked_by_default": bool(tool.blocked_by_default),
            "requires_confirm": bool(tool.requires_confirm),
        }

        if tool.blocked_by_default:
            ctx.policy_fired = "blocked_by_default"
            raise PolicyDenied(
                reason=f"'{tool.name}' 被标记为默认禁止，需手动覆盖才可执行"
            )

        tool_risk = RISK_ORDER.get(tool.risk_level, 3)  # 未知 → critical
        threshold = RISK_ORDER[self.max_risk]
        if tool_risk > threshold:
            ctx.policy_fired = "risk_threshold"
            raise PolicyDenied(
                reason=f"风险等级 {tool.risk_level} 超过会话阈值 {self.max_risk}"
            )

        if tool.requires_confirm:
            # 确认键 = (工具名, 目标路径)。工具入参已由调用方统一解析为字典，
            # 从其中读取 path 即可得本次写入目标。
            confirm_key = (tool.name, ctx.args.get("path"))
            if confirm_key not in self._confirmed:
                ctx.policy_fired = "requires_confirm"
                raise ConfirmRequired(
                    tool_name=tool.name,
                    params=ctx.args,
                    risk_level=tool.risk_level,
                    side_effects=tool.side_effects,
                    # lambda 捕获 confirm_key：回调只往已确认集合里加当前键，闭包安全
                    on_confirmed=lambda: self._confirmed.add(confirm_key),
                )
