# paperflow/core/security/middleware/policy_engine.py
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
    """策略检查中间件：默认禁止、风险阈值、确认放行三级检查。

    Attributes:
        max_risk: str，会话允许的最大风险等级（超过即拒绝）
        _confirmed: set[tuple[str, str | None]]，本会话已放行的 (工具名, 目标路径) 集合
    """

    def __init__(self, max_risk: str = "medium"):
        """指定会话风险阈值；非法阈值在构造期即失败。

        初始化已确认集合——存放本会话内用户放行过的 (工具名, 目标路径)，
        同一键不再重复询问。

        Args:
            max_risk: 会话允许的最大风险等级（"low" / "medium" / "high" / "critical"），
                      默认 "medium"。工具风险等级超过此值将被策略拒绝。

        Raises:
            ValueError: 当 max_risk 不在 RISK_ORDER 中时抛出（fail-fast）
        """
        if max_risk not in RISK_ORDER:
            raise ValueError(
                f"非法风险阈值: {max_risk}，合法值: {sorted(RISK_ORDER.keys())}"
            )
        self.max_risk = max_risk

        # 已确认集合，键为 (工具名, 目标路径)：同一工具的不同路径仍需单独确认。
        # 目标路径经 tool.effective_target_path(args) 导出：有 path 参数的工具取
        # args["path"]；有 pathless 便捷入口的工具（如 write_file 的 filename 模式）
        # 覆写为组合落盘路径——否则键会塌缩为 (工具名, None)，一次授权即放行
        # 全会话 pathless 写。返回 None 的确认工具键为 (工具名, None)，
        # 退化为旧的按工具名确认的行为（防御式）。
        self._confirmed: set[tuple[str, str | None]] = set()

    async def before(self, ctx: ToolContext) -> None:
        """按 默认禁止 → 风险阈值 → 确认放行 的顺序检查工具，违规即抛异常。

        三级检查依次进行，任一环节失败即终止后续检查并抛出对应异常：
            1. 默认禁止（blocked_by_default）：工具声明中标记为永久禁止 → PolicyDenied
            2. 风险阈值（risk_threshold）：工具风险等级 > 会话阈值 → PolicyDenied
            3. 确认放行（requires_confirm）：需要确认且未确认过 → ConfirmRequired

        只有三级检查全部通过，工具才被允许执行。

        Args:
            ctx: 工具调用上下文，包含工具定义、参数等信息

        Raises:
            PolicyDenied: 第1级或第2级检查失败时抛出
            ConfirmRequired: 第3级检查失败（需要用户确认）时抛出
        """
        # 未知工具（LLM 幻觉或注入）跳过策略检查，交给审计中间件记录错误
        if ctx.tool is None:
            return        # 未知工具交给 after 钩子做审计

        tool = ctx.tool

        # 记录本次评估的策略配置输入（供审计 replay：这条调用当时在什么配置下被评估）
        # 包含：会话阈值、工具风险等级、两个布尔标志
        ctx.policy_context = {
            "max_risk": self.max_risk,
            "tool_risk": tool.risk_level,
            "blocked_by_default": bool(tool.blocked_by_default),
            "requires_confirm": bool(tool.requires_confirm),
        }

        # ===== 第1级检查：默认禁止 =====
        # 如果工具在注册时标记为 blocked_by_default=True，意味着无论风险等级如何，
        # 该工具在默认配置下不可执行（需管理员手动覆盖策略）。
        if tool.blocked_by_default:
            ctx.policy_fired = "blocked_by_default"
            raise PolicyDenied(
                reason=f"'{tool.name}' 被标记为默认禁止，需手动覆盖才可执行"
            )

        # ===== 第2级检查：风险阈值 =====
        # 将工具的风险等级映射为数值，与会话阈值比较。
        # 未知风险等级（如拼写错误）按最严格等级（critical=3）处理，fail-safe。
        tool_risk = RISK_ORDER.get(tool.risk_level, 3)

        threshold = RISK_ORDER[self.max_risk]
        if tool_risk > threshold:
            ctx.policy_fired = "risk_threshold"
            raise PolicyDenied(
                reason=f"风险等级 {tool.risk_level} 超过会话阈值 {self.max_risk}"
            )

        # ===== 第3级检查：确认放行 =====
        if tool.requires_confirm:
            # 若工具声明 requires_confirm=True，则用户必须显式确认才能执行。
            # 确认键为 (工具名, 有效目标路径)：同一工具的不同路径需分别确认。
            # 路径经 effective_target_path 导出——pathless 入口（filename 模式）
            # 组合出落盘路径后与 path 模式同键控，不会塌缩为按工具名放行。
            confirm_key = (tool.name, tool.effective_target_path(ctx.args))
            if confirm_key not in self._confirmed:
                # 该确认键未被确认过，抛 ConfirmRequired
                ctx.policy_fired = "requires_confirm"
                raise ConfirmRequired(
                    tool_name=tool.name,
                    params=ctx.args,
                    risk_level=tool.risk_level,
                    side_effects=tool.side_effects,
                    # lambda 捕获 confirm_key：回调只往已确认集合里加当前键，闭包安全
                    on_confirmed=lambda: self._confirmed.add(confirm_key),
                )
