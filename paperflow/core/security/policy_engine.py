# paperflow/core/security/policy_engine.py
"""
PolicyEngineMiddleware —— 三级策略检查中间件。

在 ``before`` 阶段按顺序执行三级检查：

1. ``blocked_by_default``：工具被标记为默认禁止，直接抛 ``PolicyDenied``；
2. 风险阈值：工具 ``risk_level`` 超过会话阈值 ``max_risk`` 即抛
   ``PolicyDenied``（未知风险等级按 critical 处理）；
3. ``requires_confirm``：需要确认且本会话尚未确认过的工具抛
   ``ConfirmRequired``，用户调用 ``confirm()`` 后同（工具,目标路径）不再询问。
   （2026-08-06 起确认按路径作用域：write_file/edit_file 是覆盖型写操作，若只按
   工具名确认一次，后续任意 note/memory 路径都会被静默写，误覆盖用户手写笔记。）
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
        # 已确认集合：(工具名, 目标路径)。路径作用域——同工具不同路径仍需单独确认。
        # write_file/edit_file 都有 path 参数；无 path 的确认工具键为 (name, None)，
        # 退化为旧的工具名作用域行为（防御式，当前无此类工具）。
        self._confirmed: set[tuple[str, str | None]] = set()

    async def before(self, ctx: ToolContext) -> None:
        if ctx.tool is None:
            return        # 未知工具由 after 链审计
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

        if tool.requires_confirm:
            # 确认键 = (工具名, 目标路径)。ctx.args 已在 Agent._exec_tool 解析
            # 并归一化为 dict；before 钩子里 args.get("path") 可得本次写入目标。
            confirm_key = (tool.name, ctx.args.get("path"))
            if confirm_key not in self._confirmed:
                raise ConfirmRequired(
                    tool_name=tool.name,
                    params=ctx.args,
                    risk_level=tool.risk_level,
                    side_effects=tool.side_effects,
                    # lambda 捕获 confirm_key（方法内不再重绑定，闭包安全）
                    on_confirmed=lambda: self._confirmed.add(confirm_key),
                )
